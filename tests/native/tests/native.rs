use std::fs;
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use nxrs_applications::CameraProduct;
use camera::{Camera, Capture, DeviceError, Format, Frame, PixelFormat, ReplayCamera};
use storage::{read_record, FileRecorder, Storage, StorageLimits};
use transport::{Transport, UdpTransport};
use nxrs_applications::Error;
use recording_service::Error as RecordingError;

const FORMAT: Format = Format { width: 2, height: 2, pixels: PixelFormat::Gray8 };
static NEXT: AtomicU64 = AtomicU64::new(0);

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        loop {
            let path = std::env::temp_dir().join(format!("nxrs-native-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("temporary directory: {error}"),
            }
        }
    }
    fn join(&self, path: &str) -> PathBuf { self.0.join(path) }
}
impl Drop for Temp {
    fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
}

fn limits() -> StorageLimits {
    StorageLimits { queued_records: 4, max_frame_bytes: 16, max_records: 16 }
}

fn frame(sequence: u64) -> Frame<'static> {
    Frame { sequence, capture: Capture { format: FORMAT, len: 4, timestamp_ms: sequence * 10 }, bytes: &[7; 4] }
}

fn flush(storage: &mut FileRecorder) -> Result<(), storage::DeviceError> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match storage.flush() {
            Err(storage::DeviceError::Busy) if Instant::now() < deadline => thread::yield_now(),
            result => return result,
        }
    }
}

fn record_path(directory: &Path, sequence: u64) -> PathBuf {
    directory.join(format!("{sequence:020}.rcam"))
}

#[test]
fn replay_loads_actual_file_and_obeys_clock_capacity_and_restart() {
    let temp = Temp::new();
    let path = temp.join("source.raw");
    fs::write(&path, [7, 7, 7, 7, 9, 9, 9, 9]).unwrap();
    let mut camera = ReplayCamera::load(path, FORMAT, 10, 8).unwrap();
    assert_eq!(camera.frame_count(), 2);
    assert_eq!(camera.start(Format { width: 1, ..FORMAT }), Err(DeviceError::Unsupported));
    camera.start(FORMAT).unwrap();
    assert_eq!(camera.poll_frame(100, &mut [0; 3]), Err(DeviceError::Full));
    let mut bytes = [0; 4];
    assert_eq!(camera.poll_frame(100, &mut bytes).unwrap().unwrap().timestamp_ms, 100);
    assert_eq!(bytes, [7; 4]);
    assert_eq!(camera.poll_frame(109, &mut bytes), Ok(None));
    assert_eq!(camera.poll_frame(108, &mut bytes), Err(DeviceError::InvalidData));
    assert_eq!(camera.poll_frame(110, &mut bytes).unwrap().unwrap().timestamp_ms, 110);
    assert_eq!(bytes, [9; 4]);
    assert!(camera.exhausted());
    assert_eq!(camera.poll_frame(120, &mut bytes), Ok(None));
    camera.stop().unwrap();
    camera.start(FORMAT).unwrap();
    assert_eq!(camera.poll_frame(200, &mut bytes).unwrap().unwrap().timestamp_ms, 200);
    assert_eq!(bytes, [7; 4]);
    camera.stop().unwrap();
}

#[test]
fn replay_rejects_empty_truncated_and_oversized_input() {
    let temp = Temp::new();
    let path = temp.join("source.raw");
    for bytes in [vec![], vec![1; 3], vec![1; 12]] {
        fs::write(&path, bytes).unwrap();
        assert!(ReplayCamera::load(&path, FORMAT, 10, 8).is_err());
    }
    fs::write(&path, [1; 4]).unwrap();
    assert!(ReplayCamera::load(&path, FORMAT, 0, 8).is_err());
    assert!(ReplayCamera::load(&path, Format { pixels: PixelFormat::Jpeg, ..FORMAT }, 10, 8).is_err());
}

#[test]
fn disk_barrier_confirms_complete_records_and_finish_joins() {
    let temp = Temp::new();
    let directory = temp.join("records");
    let mut storage = FileRecorder::create_new(&directory, limits()).unwrap();
    storage.append(frame(1)).unwrap();
    storage.append(frame(2)).unwrap();
    flush(&mut storage).unwrap();
    let record = read_record(record_path(&directory, 2), 16).unwrap();
    assert_eq!(record.sequence, 2);
    assert_eq!(record.capture, frame(2).capture);
    assert_eq!(record.bytes, [7; 4]);
    // Closing without another flush must still drain accepted work and join.
    storage.append(frame(3)).unwrap();
    storage.finish().unwrap();
    assert_eq!(read_record(record_path(&directory, 3), 16).unwrap().sequence, 3);
    assert_eq!(fs::read_dir(&directory).unwrap().count(), 3);
}

#[test]
fn existing_session_and_committed_record_are_never_overwritten() {
    let temp = Temp::new();
    let directory = temp.join("records");
    let mut storage = FileRecorder::create_new(&directory, limits()).unwrap();
    assert!(FileRecorder::create_new(&directory, limits()).is_err());
    let committed = record_path(&directory, 1);
    fs::write(&committed, b"existing record").unwrap();
    storage.append(frame(1)).unwrap();
    assert_eq!(flush(&mut storage), Err(storage::DeviceError::Io));
    assert_eq!(fs::read(committed).unwrap(), b"existing record");
    assert!(!directory.join("00000000000000000001.part").exists());
    assert_eq!(storage.append(frame(2)), Err(storage::DeviceError::Io));
    assert_eq!(storage.finish(), Err(storage::DeviceError::Io));
}

#[test]
fn disk_adapter_rejects_invalid_frames_and_enforces_quota() {
    let temp = Temp::new();
    let directory = temp.join("records");
    let mut storage = FileRecorder::create_new(&directory, StorageLimits { max_records: 1, ..limits() }).unwrap();
    assert_eq!(storage.append(frame(0)), Err(storage::DeviceError::InvalidData));
    assert_eq!(storage.append(Frame { bytes: &[7; 3], ..frame(1) }), Err(storage::DeviceError::InvalidData));
    storage.append(frame(1)).unwrap();
    assert_eq!(storage.append(frame(2)), Err(storage::DeviceError::Full));
    flush(&mut storage).unwrap();
    storage.finish().unwrap();
    assert_eq!(fs::read_dir(directory).unwrap().count(), 1);
}

#[test]
fn record_reader_rejects_truncation_trailing_data_and_large_allocations() {
    let temp = Temp::new();
    let directory = temp.join("records");
    let mut storage = FileRecorder::create_new(&directory, limits()).unwrap();
    storage.append(frame(1)).unwrap();
    storage.finish().unwrap();
    let original = fs::read(record_path(&directory, 1)).unwrap();
    let bad = temp.join("bad.rcam");
    fs::write(&bad, &original[..original.len() - 1]).unwrap();
    assert!(read_record(&bad, 16).is_err());
    let mut trailing = original.clone();
    trailing.push(0);
    fs::write(&bad, trailing).unwrap();
    assert!(read_record(&bad, 16).is_err());
    assert!(read_record(record_path(&directory, 1), 3).is_err());
    let mut oversized = original;
    oversized[32..40].copy_from_slice(&u64::MAX.to_le_bytes());
    fs::write(&bad, oversized).unwrap();
    assert!(read_record(&bad, 16).is_err());
}

#[test]
fn udp_uses_real_loopback_datagrams_with_independent_owners() {
    let first = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second = UdpSocket::bind("127.0.0.1:0").unwrap();
    first.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    second.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut a = UdpTransport::bind("127.0.0.1:0".parse().unwrap(), first.local_addr().unwrap()).unwrap();
    let mut b = UdpTransport::bind("127.0.0.1:0".parse().unwrap(), second.local_addr().unwrap()).unwrap();
    assert_ne!(a.local_addr().unwrap(), b.local_addr().unwrap());
    a.send(b"first").unwrap();
    b.send(b"second").unwrap();
    let mut buffer = [0; 16];
    let len = first.recv(&mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"first");
    let len = second.recv(&mut buffer).unwrap();
    assert_eq!(&buffer[..len], b"second");
    assert_eq!(a.send(&[]), Err(transport::DeviceError::InvalidData));
    assert_eq!(a.send(&vec![0; 65_508]), Err(transport::DeviceError::InvalidData));
}

#[test]
fn unchanged_applications_compose_real_replay_disk_and_udp_adapters() {
    let temp = Temp::new();
    let source = temp.join("source.raw");
    fs::write(&source, [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3]).unwrap();
    let directory = temp.join("records");
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let camera = ReplayCamera::load(source, FORMAT, 10, 12).unwrap();
    let storage = FileRecorder::create_new(&directory, limits()).unwrap();
    let transport = UdpTransport::bind("127.0.0.1:0".parse().unwrap(), receiver.local_addr().unwrap()).unwrap();
    let mut product = CameraProduct::<_, _, _, 16, 2>::new(camera, storage, transport).unwrap();
    product.camera.start(FORMAT).unwrap();
    product.recorder.start(&product.camera).unwrap();
    product.monitor.start(&product.camera).unwrap();
    assert!(product.step(0).recorder.is_ok());
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match product.recorder.stop(&mut product.recordings) {
            Err(Error::Recording(RecordingError::Device(storage::DeviceError::Busy))) if Instant::now() < deadline => thread::yield_now(),
            result => { result.unwrap(); break; }
        }
    }
    assert!(product.step(10).monitor.is_ok());
    product.recorder.start(&product.camera).unwrap();
    assert!(product.step(20).recorder.is_ok());
    assert_eq!(product.recorder.stats().processed, 2);
    assert_eq!(product.monitor.stats().processed, 3);
    loop {
        let report = product.shutdown();
        report.camera.unwrap();
        match report.recorder {
            Err(Error::Recording(RecordingError::Device(storage::DeviceError::Busy))) if Instant::now() < deadline => thread::yield_now(),
            result => { result.unwrap(); break; }
        }
    }
    drop(product); // joins the actual filesystem worker
    assert_eq!(read_record(record_path(&directory, 1), 16).unwrap().bytes, [1; 4]);
    assert_eq!(read_record(record_path(&directory, 3), 16).unwrap().bytes, [3; 4]);
    assert!(!record_path(&directory, 2).exists());
    let mut sequences = Vec::new();
    for _ in 0..3 {
        let mut bytes = [0; 64];
        assert_eq!(receiver.recv(&mut bytes).unwrap(), 28);
        sequences.push(u64::from_le_bytes(bytes[8..16].try_into().unwrap()));
    }
    sequences.sort_unstable();
    assert_eq!(sequences, [1, 2, 3]);
}

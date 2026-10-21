use std::fs;
use std::net::UdpSocket;
use std::thread;
use std::time::{Duration, Instant};

use nxrs_applications::{CameraProduct, Error as AppError};
use camera::{Camera, Capture, DeviceError, Format, PixelFormat, ReplayCamera};
use storage::{read_record, FileRecorder, StorageLimits};
use transport::UdpTransport;
use camera_service::{CameraService, Error as CameraError, Frames};
use recording_service::Error as RecordingError;

fn frame_buffer(bytes: usize) -> std::io::Result<Box<[u8]>> {
    let mut buffer = Vec::new();
    buffer
        .try_reserve_exact(bytes)
        .map_err(|_| std::io::Error::other("frame buffer allocation failed"))?;
    buffer.resize(bytes, 0);
    Ok(buffer.into_boxed_slice())
}

#[test]
fn full_sized_native_product_runs_on_a_128_kib_stack() {
    let root = std::env::temp_dir().join(format!("nxrs-pool-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let source = root.join("source.gray");
    fs::write(&source, vec![0x51; 65_536]).unwrap();
    let directory = root.join("records");
    let worker_directory = directory.clone();
    thread::Builder::new().stack_size(128 * 1024).spawn(move || {
        let format = Format { width: 256, height: 256, pixels: PixelFormat::Gray8 };
        let camera = ReplayCamera::load(source, format, 10, 65_536).unwrap();
        let storage = FileRecorder::create_new(&worker_directory, StorageLimits {
            queued_records: 1, max_frame_bytes: 65_536, max_records: 1,
        }).unwrap();
        let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
        receiver.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let transport = UdpTransport::bind("127.0.0.1:0".parse().unwrap(), receiver.local_addr().unwrap()).unwrap();
        let history = [frame_buffer(65_536).unwrap(), frame_buffer(65_536).unwrap()];
        let mut product = CameraProduct::<_, _, _, 65_536, 2, _>::with_buffers(
            camera, storage, transport, history, frame_buffer(65_536).unwrap(),
        ).unwrap();
        assert!(std::mem::size_of_val(&product) < 4096, "payload leaked into inline product");
        product.camera.start(format).unwrap();
        product.recorder.start(&product.camera).unwrap();
        product.monitor.start(&product.camera).unwrap();
        let report = product.step(0);
        report.camera.unwrap();
        report.recorder.unwrap();
        report.monitor.unwrap();
        assert_eq!(product.camera.next_after(0).unwrap().bytes, vec![0x51; 65_536]);
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let report = product.shutdown();
            report.camera.unwrap();
            match report.recorder {
                Err(AppError::Recording(RecordingError::Device(storage::DeviceError::Busy))) if Instant::now() < deadline => thread::yield_now(),
                result => { result.unwrap(); break; }
            }
        }
        drop(product);
        assert_eq!(receiver.recv(&mut [0; 64]).unwrap(), 28);
    }).unwrap().join().unwrap();
    let record = read_record(directory.join("00000000000000000001.rcam"), 65_536).unwrap();
    assert_eq!(record.bytes, vec![0x51; 65_536]);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn externally_placed_slices_are_validated_before_device_start() {
    // An inert device is sufficient: construction must not start any hardware.
    struct Device;
    impl Camera for Device {
        fn start(&mut self, _: Format) -> Result<Format, DeviceError> { panic!("constructor started device") }
        fn poll_frame(&mut self, _: u64, _: &mut [u8]) -> Result<Option<Capture>, DeviceError> { unreachable!() }
        fn stop(&mut self) -> Result<(), DeviceError> { Ok(()) }
    }
    let mut history = [0u8; 4];
    let mut staging = [0u8; 4];
    let service = CameraService::<_, 4, 1, &mut [u8]>::with_buffers(
        Device, [&mut history[..]], &mut staging[..],
    ).unwrap();
    assert_eq!(service.latest_sequence(), 0);
    drop(service);
    assert!(matches!(CameraService::<_, 4, 1, &mut [u8]>::with_buffers(
        Device, [&mut history[..]], &mut staging[..3],
    ), Err(CameraError::InvalidCapacity)));
}

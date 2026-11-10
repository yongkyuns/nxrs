//! Real shared recording, exercised with unset, mock and native profiles.
use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use storage::recording::{read_record, FileRecorder, StorageLimits};
use storage::{Capture, DeviceError, Format, Frame, PixelFormat, Storage};

static NEXT: AtomicU64 = AtomicU64::new(0);
const FORMAT: Format = Format {
    width: 2,
    height: 2,
    pixels: PixelFormat::Gray8,
};

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        for _ in 0..32 {
            let path = std::env::temp_dir().join(format!(
                "nxrs-std-recording-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed),
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("temporary directory: {error}"),
            }
        }
        panic!("could not allocate a unique test directory");
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn frame(sequence: u64, bytes: &[u8]) -> Frame<'_> {
    Frame {
        sequence,
        capture: Capture {
            format: FORMAT,
            len: bytes.len(),
            timestamp_ms: sequence * 10,
        },
        bytes,
    }
}

#[test]
fn shared_recording_commits_owned_frames_and_joins_without_hardware_admission() {
    let temp = Temp::new();
    let directory = temp.0.join("records");
    let limits = StorageLimits {
        queued_records: 2,
        max_frame_bytes: 4,
        max_records: 2,
    };
    let mut recorder = FileRecorder::create_new(&directory, limits).unwrap();
    assert_eq!(
        FileRecorder::create_new(&directory, limits)
            .err()
            .expect("existing session must not be overwritten")
            .kind(),
        io::ErrorKind::AlreadyExists,
    );
    let mut bytes = [7; 4];
    recorder.append(frame(1, &bytes)).unwrap();
    bytes.fill(9);
    recorder.append(frame(2, &bytes)).unwrap();
    bytes.fill(0);
    assert_eq!(recorder.append(frame(3, &bytes)), Err(DeviceError::Full));
    recorder.finish().unwrap(); // Completion joins the writer before inspection.

    for (sequence, expected) in [(1, 7), (2, 9)] {
        let record = read_record(directory.join(format!("{sequence:020}.rcam")), 4).unwrap();
        assert_eq!(record.sequence, sequence);
        assert_eq!(record.capture.format, FORMAT);
        assert_eq!(record.capture.timestamp_ms, sequence * 10);
        assert_eq!(record.capture.len, 4);
        assert_eq!(record.bytes, [expected; 4]);
    }
    assert_eq!(fs::read_dir(directory).unwrap().count(), 2);
}

#[test]
fn explicit_recording_constructor_flushes_a_real_record() {
    let temp = Temp::new();
    let directory = temp.0.join("factory");
    let mut recorder = FileRecorder::create_new(
        &directory,
        StorageLimits {
            queued_records: 1,
            max_frame_bytes: 4,
            max_records: 1,
        },
    )
    .unwrap();
    recorder.append(frame(1, &[5; 4])).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match recorder.flush() {
            Ok(()) => break,
            Err(DeviceError::Busy) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(1));
            }
            result => panic!("record flush did not complete: {result:?}"),
        }
    }
    let record = read_record(directory.join("00000000000000000001.rcam"), 4).unwrap();
    assert_eq!(record.bytes, [5; 4]);
    drop(recorder);
}

#[test]
fn invalid_limits_are_rejected_before_creating_a_directory() {
    let temp = Temp::new();
    let directory = temp.0.join("invalid");
    let result = FileRecorder::create_new(
        &directory,
        StorageLimits {
            queued_records: 0,
            max_frame_bytes: 4,
            max_records: 1,
        },
    );
    assert_eq!(
        result.err().expect("zero queue is invalid").kind(),
        io::ErrorKind::InvalidInput
    );
    assert!(!directory.exists());
}

#[cfg(not(platform = "nuttx"))]
#[test]
fn unbound_record_file_acquisition_does_not_substitute_directory_recording() {
    let temp = Temp::new();
    let directory = temp.0.join("not-admitted");
    let result = storage::create_record_file(directory.to_str().unwrap(), 1, 4);
    assert_eq!(
        result
            .err()
            .expect("record-file acquisition must be explicit"),
        DeviceError::Unsupported
    );
    assert!(!directory.exists());
}

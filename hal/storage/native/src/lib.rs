//! Bounded frame-record sink using std files and a std worker thread.
#![forbid(unsafe_code)]
#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
compile_error!("native storage supports Linux, macOS and Windows only");

mod recording;
pub use recording::{read_record, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES};
use std::io;
use storage_api::{DeviceError, Storage};

pub fn storage(
    directory: &str,
    queued_records: usize,
    max_frame_bytes: usize,
    max_records: u64,
) -> io::Result<impl Storage> {
    FileRecorder::create_new(directory, StorageLimits {
        queued_records,
        max_frame_bytes,
        max_records,
    })
}

fn device_error(error: io::Error) -> DeviceError {
    // File-worker errors are terminal; Busy belongs to the live queue/barrier.
    match error.kind() {
        io::ErrorKind::TimedOut => DeviceError::Timeout,
        io::ErrorKind::Unsupported => DeviceError::Unsupported,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

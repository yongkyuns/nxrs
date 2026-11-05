//! Bounded frame-record sink using std files and a std worker thread.
#![forbid(unsafe_code)]

mod writer;
use crate::DeviceError;
use std::io;
pub use writer::{read_record, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES};

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

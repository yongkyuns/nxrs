//! Frame-record acceptance and flush contract; not a general filesystem API.
#![no_std]
#![forbid(unsafe_code)]

pub use camera_api::{Capture, DeviceError, Format, Frame, PixelFormat};

/// Atomic record acceptance: Err accepts nothing, including Busy/Full.
/// Physical adapters must implement this contract (e.g. committed records),
/// not expose arbitrary partial file writes as successful records.
pub trait Storage {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError>;
    fn flush(&mut self) -> Result<(), DeviceError>;
}

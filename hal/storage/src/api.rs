//! Frame-record acceptance and flush contract; not a general filesystem API.
#![forbid(unsafe_code)]

use camera::{DeviceError, Frame};

/// Atomic record acceptance: Err accepts nothing, including Busy/Full.
/// Physical adapters must implement this contract (e.g. committed records),
/// not expose arbitrary partial file writes as successful records.
pub trait Storage {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError>;
    fn flush(&mut self) -> Result<(), DeviceError>;
}

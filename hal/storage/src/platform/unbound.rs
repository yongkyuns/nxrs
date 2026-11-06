//! Unavailable synchronous record-file acquisition. Never fall back to other I/O.
use crate::{DeviceError, Frame, Storage};

struct Unconfigured;
impl Storage for Unconfigured {
    fn append(&mut self, _: Frame<'_>) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn flush(&mut self) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

pub fn create_record_file(_: &str, _: u64, _: usize) -> Result<impl Storage, DeviceError> {
    Err::<Unconfigured, _>(DeviceError::Unsupported)
}

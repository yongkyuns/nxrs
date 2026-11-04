//! Unavailable acquisition; no replay or implicit mock fallback.
use crate::{Camera, Capture, DeviceError, Format};

struct UnconfiguredCamera;

impl Camera for UnconfiguredCamera {
    fn start(&mut self, _: Format) -> Result<Format, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn poll_frame(&mut self, _: u64, _: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        Ok(())
    }
}

pub fn open() -> Result<impl Camera, DeviceError> {
    Err::<UnconfiguredCamera, _>(DeviceError::Unsupported)
}

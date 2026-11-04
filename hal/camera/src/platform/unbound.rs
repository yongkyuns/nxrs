use crate::{Camera, Capture, DeviceError, Format};
pub struct UnconfiguredCamera;

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

pub fn open() -> Result<UnconfiguredCamera, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(any(
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "nuttx"
))]
#[path = "../replay/unavailable.rs"]
mod replay;
#[cfg(any(
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "nuttx"
))]
pub use replay::replay;

//! Camera contract, explicit replay and OS camera acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test))]
extern crate std;

mod api;
pub use api::{Camera, Capture, DeviceError, Format, Frame, JpegQuality, PixelFormat};
#[cfg(any(hal_enabled, test))]
#[cfg_attr(test, allow(dead_code))]
mod replay;
#[cfg(hal_enabled)]
pub use replay::camera as replay;
#[cfg(hal_enabled)]
pub use replay::{camera, ReplayCamera};
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(hal_mock)]
pub use mock::{CameraAction, MockCamera, FORMAT};
#[cfg(hal_mock)]
pub fn open() -> Result<impl Camera, DeviceError> {
    Ok(MockCamera::new([]))
}
#[cfg(any(hal_enabled, all(test, unix)))]
mod platform;
#[cfg(hal_enabled)]
pub use platform::open;
#[cfg(not(any(hal_enabled, hal_mock)))]
pub struct UnconfiguredCamera;

#[cfg(not(any(hal_enabled, hal_mock)))]
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

#[cfg(not(any(hal_enabled, hal_mock)))]
pub fn open() -> Result<UnconfiguredCamera, DeviceError> {
    Err(DeviceError::Unsupported)
}

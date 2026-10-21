//! Camera capability contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(
    nxrs_hal_platform = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate alloc;
#[cfg(any(
    nxrs_hal_platform = "std",
    all(nxrs_hal_platform = "nuttx", target_os = "nuttx"),
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{Camera, Capture, DeviceError, Format, Frame, JpegQuality, PixelFormat};

#[cfg(any(
    nxrs_hal_platform = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(nxrs_hal_platform = "mock")]
pub use mock::{CameraAction, MockCamera, FORMAT};

#[cfg(any(
    nxrs_hal_platform = "std",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/std.rs"]
mod standard;
#[cfg(nxrs_hal_platform = "std")]
pub use standard::{camera, ReplayCamera};

#[cfg(any(
    all(test, any(target_os = "linux", target_os = "macos")),
    all(nxrs_hal_platform = "nuttx", target_os = "nuttx")
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/nuttx/mod.rs"]
mod v4l2;

#[cfg(nxrs_hal_platform = "std")]
pub use standard::camera as replay;

/// Acquire the platform's live camera without exposing a device path or driver.
#[cfg(all(nxrs_hal_platform = "nuttx", target_os = "nuttx"))]
pub fn open() -> Result<impl Camera, DeviceError> {
    v4l2::LiveCamera::acquire()
}

#[cfg(nxrs_hal_platform = "mock")]
pub fn open() -> Result<impl Camera, DeviceError> {
    Ok(MockCamera::new([]))
}

/// Desktop file replay is explicit input, not a claimed physical-camera driver.
#[cfg(nxrs_hal_platform = "std")]
pub fn open() -> Result<impl Camera, DeviceError> {
    Err::<ReplayCamera, _>(DeviceError::Unsupported)
}

#[cfg(nxrs_hal_platform = "none")]
pub struct UnconfiguredCamera;

#[cfg(nxrs_hal_platform = "none")]
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

#[cfg(nxrs_hal_platform = "none")]
pub fn open() -> Result<UnconfiguredCamera, DeviceError> {
    Err(DeviceError::Unsupported)
}

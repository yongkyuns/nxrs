//! Camera capability contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(
    nxrs_provider = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate alloc;
#[cfg(any(
    nxrs_provider = "native",
    all(nxrs_provider = "nuttx", target_os = "nuttx"),
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{Camera, Capture, DeviceError, Format, Frame, JpegQuality, PixelFormat};

#[cfg(any(
    nxrs_provider = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "providers/mock.rs"]
mod mock;
#[cfg(nxrs_provider = "mock")]
pub use mock::{CameraAction, MockCamera, FORMAT};

#[cfg(any(
    nxrs_provider = "native",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "providers/native.rs"]
mod native;
#[cfg(nxrs_provider = "native")]
pub use native::{camera, ReplayCamera};

#[cfg(nxrs_provider = "nuttx")]
#[allow(unsafe_code)]
#[path = "providers/nuttx.rs"]
mod nuttx;
#[cfg(nxrs_provider = "nuttx")]
pub use nuttx::DeviceCamera;

#[cfg(any(
    all(test, any(target_os = "linux", target_os = "macos")),
    all(nxrs_provider = "nuttx", target_os = "nuttx")
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "providers/v4l2.rs"]
mod v4l2;

#[cfg(nxrs_provider = "native")]
pub use native::camera as replay;

/// Acquire the platform's live camera without exposing a device path or driver.
#[cfg(all(nxrs_provider = "nuttx", target_os = "nuttx"))]
pub fn open() -> Result<impl Camera, DeviceError> {
    v4l2::LiveCamera::acquire()
}

#[cfg(nxrs_provider = "mock")]
pub fn open() -> Result<impl Camera, DeviceError> {
    Ok(MockCamera::new([]))
}

/// Desktop file replay is explicit input, not a claimed physical-camera driver.
#[cfg(nxrs_provider = "native")]
pub fn open() -> Result<impl Camera, DeviceError> {
    Err::<ReplayCamera, _>(DeviceError::Unsupported)
}

#[cfg(any(
    nxrs_provider = "none",
    all(nxrs_provider = "nuttx", target_os = "none")
))]
pub struct UnconfiguredCamera;

#[cfg(any(
    nxrs_provider = "none",
    all(nxrs_provider = "nuttx", target_os = "none")
))]
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

#[cfg(any(
    nxrs_provider = "none",
    all(nxrs_provider = "nuttx", target_os = "none")
))]
pub fn open() -> Result<UnconfiguredCamera, DeviceError> {
    Err(DeviceError::Unsupported)
}

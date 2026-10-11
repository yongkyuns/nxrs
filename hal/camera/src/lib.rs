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
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{Camera, Capture, DeviceError, Format, Frame, PixelFormat};

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

#[cfg(nxrs_provider = "native")]
pub use native::camera as open;

#[cfg(nxrs_provider = "nuttx")]
pub fn open(path: &core::ffi::CStr) -> impl Camera + '_ {
    DeviceCamera::new(path)
}

#[cfg(nxrs_provider = "mock")]
pub fn open(actions: impl IntoIterator<Item = CameraAction>) -> impl Camera {
    MockCamera::new(actions)
}

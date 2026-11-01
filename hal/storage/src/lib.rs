//! Frame recording and platform acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(
    hal_enabled,
    test,
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
extern crate std;

mod api;
pub use api::{DeviceError, Storage};
pub use camera::{Capture, Format, Frame, PixelFormat};
#[cfg(any(all(hal_enabled, target_os = "nuttx"), test))]
#[cfg_attr(test, allow(dead_code))]
mod file;
mod platform;
#[cfg(any(
    hal_enabled,
    test,
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
#[allow(dead_code)]
mod record;
#[cfg(any(
    hal_enabled,
    test,
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
#[allow(dead_code)]
mod recording;
#[cfg(any(hal_enabled, hal_mock, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use platform::*;

//! Camera contract, explicit replay and capability-local acquisition.
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
pub use api::{Camera, Capture, DeviceError, Format, Frame, JpegQuality, PixelFormat};
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
mod replay;
pub use platform::*;

#[cfg(all(test, unix, not(target_os = "nuttx")))]
#[allow(dead_code, unused_imports)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx_tests;

#[cfg(all(test, not(hal_mock)))]
#[allow(dead_code, unused_imports)]
#[path = "platform/mock.rs"]
mod mock_tests;
#[cfg(all(test, hal_mock))]
use platform as mock_tests;

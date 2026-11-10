//! Camera contracts and configured acquisition.
//!
//! The optional `replay` Cargo feature adds explicit file replay independently
//! of hardware admission. `open()` never substitutes replay for a live device.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_provider = "mock", test))]
extern crate alloc;
#[cfg(any(hal_provider = "nuttx", feature = "replay", test))]
extern crate std;

mod api;
mod platform;
pub use api::{Camera, Capture, DeviceError, Format, Frame, JpegQuality, PixelFormat};

/// Acquire the configured camera, or report that acquisition is unavailable.
/// The implementation type is private on every platform.
pub fn open() -> Result<impl Camera, DeviceError> {
    platform::open()
}

cfg_if::cfg_if! {
    if #[cfg(feature = "replay")] {
        pub mod replay;
        // These names always refer to explicit file replay.
        pub use replay::{open as camera, open as replay, ReplayCamera};
    }
}

// Scripted fixtures are explicitly selected, never an implicit live fallback.
#[cfg(hal_provider = "mock")]
pub use platform::{CameraAction, MockCamera, FORMAT};

#[cfg(test)]
mod test_support;
#[cfg(test)]
#[allow(unused_imports)]
use test_support::mock_tests;

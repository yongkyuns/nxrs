//! Frame recording and platform acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test))]
extern crate std;

mod api;
pub use api::{DeviceError, Storage};
pub use camera::{Capture, Format, Frame, PixelFormat};
#[cfg(any(all(hal_enabled, target_os = "nuttx"), test))]
#[cfg_attr(test, allow(dead_code))]
mod file;
#[cfg(any(hal_enabled, hal_mock, test))]
mod platform;
#[cfg(any(hal_enabled, test))]
mod record;
#[cfg(any(hal_enabled, test))]
#[cfg_attr(test, allow(dead_code))]
mod recording;
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::*;

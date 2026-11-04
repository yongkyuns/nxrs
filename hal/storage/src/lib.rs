//! Frame recording and platform acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
extern crate std;

mod api;
pub use api::{DeviceError, Storage};
pub use camera::{Capture, Format, Frame, PixelFormat};
#[cfg(any(all(hal_enabled, target_os = "nuttx"), test))]
#[cfg_attr(test, allow(dead_code))]
mod file;
mod platform;
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
#[allow(dead_code)]
mod record;
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub mod recording;

// Explicit shared functionality is independent of hardware admission.
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use recording::{read_record, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES};

// Legacy factory signatures and admission behavior remain unchanged.
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::open;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use platform::FileStorage;
#[cfg(hal_mock)]
pub use platform::{MockStorage, StoredFrame};
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use platform::storage;

#[cfg(test)]
mod test_support;

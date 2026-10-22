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
#[cfg(any(hal_enabled, test))]
mod record;
#[cfg(any(hal_enabled, test))]
#[cfg_attr(test, allow(dead_code))]
mod recording;
#[cfg(hal_enabled)]
pub use recording::{
    read_record, storage, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES,
};
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(hal_mock)]
pub use mock::{MockStorage, StoredFrame};
#[cfg(hal_mock)]
pub fn open() -> impl Storage {
    MockStorage::default()
}
#[cfg(any(hal_enabled, all(test, unix)))]
mod platform;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use platform::nuttx::FileStorage;
#[cfg(hal_enabled)]
pub use platform::open;

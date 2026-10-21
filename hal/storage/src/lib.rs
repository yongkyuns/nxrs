//! Frame-record storage contract and platform-selected provider.
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
    nxrs_hal_platform = "nuttx",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{DeviceError, Storage};
pub use camera::{Capture, Format, Frame, PixelFormat};

#[cfg(any(nxrs_hal_platform = "std", nxrs_hal_platform = "nuttx", test))]
mod record;

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
pub use mock::{MockStorage, StoredFrame};

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
pub use standard::{
    read_record, storage, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES,
};

#[cfg(any(nxrs_hal_platform = "nuttx", test))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/file.rs"]
mod file;

#[cfg(any(nxrs_hal_platform = "nuttx", all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/nuttx.rs"]
mod nuttx;
#[cfg(nxrs_hal_platform = "nuttx")]
pub use nuttx::FileStorage;

#[cfg(nxrs_hal_platform = "std")]
pub use standard::storage as open;

#[cfg(nxrs_hal_platform = "nuttx")]
pub fn open(path: &str, max_records: u64, max_frame: usize) -> Result<impl Storage, DeviceError> {
    FileStorage::create(path, max_records, max_frame)
}

#[cfg(nxrs_hal_platform = "mock")]
pub fn open() -> impl Storage {
    MockStorage::default()
}

//! Frame-record storage contract and platform-selected provider.
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
pub use api::Storage;
pub use camera::{Capture, DeviceError, Format, Frame, PixelFormat};

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
pub use mock::{MockStorage, StoredFrame};

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
pub use native::{
    read_record, storage, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES,
};

#[cfg(nxrs_provider = "nuttx")]
#[allow(unsafe_code)]
#[path = "providers/nuttx.rs"]
mod nuttx;
#[cfg(nxrs_provider = "nuttx")]
pub use nuttx::FileStorage;

#[cfg(nxrs_provider = "native")]
pub use native::storage as open;

#[cfg(nxrs_provider = "nuttx")]
pub fn open(
    path: &core::ffi::CStr,
    max_records: u64,
    max_frame: usize,
) -> Result<impl Storage, DeviceError> {
    FileStorage::create(path, max_records, max_frame)
}

#[cfg(nxrs_provider = "mock")]
pub fn open() -> impl Storage {
    MockStorage::default()
}

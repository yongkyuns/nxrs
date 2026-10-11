//! Storage capability facade. Concrete provider selection stays inside this HAL.
#![no_std]
#![forbid(unsafe_code)]

pub use storage_api::{Capture, DeviceError, Format, Frame, PixelFormat, Storage};

#[cfg(any(
    all(feature = "mock", feature = "native"),
    all(feature = "mock", feature = "nuttx"),
    all(feature = "native", feature = "nuttx"),
))]
compile_error!("select at most one nxrs-storage provider feature");

#[cfg(feature = "native")]
pub use storage_native::storage as open;

#[cfg(feature = "nuttx")]
pub fn open(
    path: &core::ffi::CStr,
    max_records: u64,
    max_frame: usize,
) -> Result<impl Storage, DeviceError> {
    storage_nuttx::FileStorage::create(path, max_records, max_frame)
}

#[cfg(feature = "mock")]
pub fn open() -> impl Storage {
    storage_mock::MockStorage::default()
}

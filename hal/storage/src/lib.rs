//! Frame recording contracts, explicit software recording and record-file acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(platform = "mock", test))]
extern crate alloc;
cfg_if::cfg_if! {
    if #[cfg(any(platform = "nuttx", feature = "recording", test))] {
        extern crate std;
        mod record;
    }
}

mod api;
pub use api::{DeviceError, Storage};
pub use camera::{Capture, Format, Frame, PixelFormat};
#[cfg(any(platform = "nuttx", test))]
#[cfg_attr(test, allow(dead_code))]
mod file;
mod platform;
#[cfg(feature = "recording")]
pub mod recording;

/// Create one synchronous record file through the configured acquisition.
///
/// This operation has a fixed signature and append/rollback/file-sync contract.
/// An unbound, mock or unsupported provider reports `Unsupported`; it never
/// substitutes queued directory recording or a scripted fixture.
pub fn create_record_file(
    path: &str,
    max_records: u64,
    max_frame_bytes: usize,
) -> Result<impl Storage, DeviceError> {
    platform::create_record_file(path, max_records, max_frame_bytes)
}

#[cfg(platform = "mock")]
pub use platform::{MockStorage, StoredFrame};

#[cfg(test)]
mod test_support;

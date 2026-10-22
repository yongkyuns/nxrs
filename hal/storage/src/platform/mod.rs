//! OS acquisition only; record formats, queues and rollback are shared Rust.
#[cfg(any(target_os = "nuttx", all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
pub(crate) mod nuttx;
#[cfg(not(target_os = "nuttx"))]
#[cfg_attr(test, allow(unused_imports))]
pub use crate::recording::storage as open;
#[cfg(target_os = "nuttx")]
pub fn open(
    path: &str,
    max_records: u64,
    max_frame: usize,
) -> Result<impl crate::Storage, crate::DeviceError> {
    nuttx::FileStorage::create(path, max_records, max_frame)
}

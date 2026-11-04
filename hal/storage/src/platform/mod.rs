//! Select acquisition; enumerate the legacy factory and fixture exports explicitly.
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(all(hal_enabled, not(target_os = "nuttx")), path = "../acquisition.rs")]
#[cfg_attr(hal_mock, path = "mock.rs")]
#[cfg_attr(not(any(hal_enabled, hal_mock)), path = "unbound.rs")]
mod selected;
#[cfg(any(hal_enabled, hal_mock))]
pub use selected::open;
#[cfg(hal_enabled)]
pub use selected::{
    read_record, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES,
};
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use selected::FileStorage;
#[cfg(hal_mock)]
pub use selected::{MockStorage, StoredFrame};
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use selected::storage;

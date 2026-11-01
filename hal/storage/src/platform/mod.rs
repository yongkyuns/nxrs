//! Admission and OS factories; record formats, queues and rollback stay shared.
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
mod mock;
#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
mod nuttx;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::{MockStorage, StoredFrame};
    pub fn open() -> impl crate::Storage {
        MockStorage::default()
    }
}
#[cfg(hal_enabled)]
mod selected {
    #[cfg(target_os = "nuttx")]
    pub use super::nuttx::{open, FileStorage};
    #[cfg(not(target_os = "nuttx"))]
    pub use crate::recording::storage as open;
    pub use crate::recording::{
        read_record, storage, FileRecorder, RecordedFrame, StorageLimits, RECORD_HEADER_BYTES,
    };
}
#[cfg(any(hal_enabled, hal_mock))]
pub use selected::*;

#[cfg(all(
    not(hal_enabled),
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
mod unbound;
#[cfg(all(
    not(hal_enabled),
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
pub use unbound::*;

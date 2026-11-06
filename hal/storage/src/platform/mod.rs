//! Select only synchronous record-file acquisition; queued recording is explicit.
#[cfg_attr(hal_mock, path = "mock.rs")]
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(
    not(any(hal_mock, all(hal_enabled, target_os = "nuttx"))),
    path = "unbound.rs"
)]
mod selected;
pub(super) use selected::create_record_file;
#[cfg(hal_mock)]
pub use selected::{MockStorage, StoredFrame};

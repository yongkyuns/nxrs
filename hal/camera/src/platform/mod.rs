//! Select configured acquisition only. Replay is an independent Rust module.
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(hal_mock, path = "mock.rs")]
#[cfg_attr(
    not(any(all(hal_enabled, target_os = "nuttx"), hal_mock)),
    path = "unbound.rs"
)]
mod selected;
pub(super) use selected::open;
#[cfg(hal_mock)]
pub use selected::{CameraAction, MockCamera, FORMAT};

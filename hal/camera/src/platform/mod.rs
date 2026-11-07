//! Select configured acquisition only. Replay is an independent Rust module.
#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[cfg_attr(hal_provider = "mock", path = "mock.rs")]
#[cfg_attr(
    not(any(
        all(hal_provider = "hardware", target_os = "nuttx"),
        hal_provider = "mock"
    )),
    path = "unbound.rs"
)]
mod selected;
pub(super) use selected::open;
#[cfg(hal_provider = "mock")]
pub use selected::{CameraAction, MockCamera, FORMAT};

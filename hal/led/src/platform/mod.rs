//! Select one implementation from profile admission and Rust target_os.
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(hal_mock, path = "mock.rs")]
#[cfg_attr(not(any(hal_enabled, hal_mock)), path = "unbound.rs")]
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), allow(unsafe_code))]
mod selected;
pub use selected::open;
#[cfg(hal_mock)]
pub use selected::MockLeds;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use selected::UserLeds;

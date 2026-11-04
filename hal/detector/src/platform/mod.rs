//! Select one implementation from profile admission, model features and Rust target_os.
#[cfg(all(hal_enabled, any(feature = "person-model", feature = "pico-model")))]
#[cfg_attr(target_os = "linux", path = "linux/mod.rs")]
#[cfg_attr(target_os = "macos", path = "macos/mod.rs")]
#[cfg_attr(target_os = "windows", path = "windows/mod.rs")]
#[cfg_attr(target_os = "nuttx", path = "nuttx/mod.rs")]
mod selected;
#[cfg(not(all(hal_enabled, any(feature = "person-model", feature = "pico-model"))))]
#[path = "unbound.rs"]
mod selected;
pub use selected::*;

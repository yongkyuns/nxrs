//! Select one implementation from profile admission and Rust target_os.
#[cfg_attr(all(hal_enabled, target_os = "linux"), path = "linux/mod.rs")]
#[cfg_attr(all(hal_enabled, target_os = "macos"), path = "macos/mod.rs")]
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(not(hal_enabled), path = "unbound.rs")]
mod selected;
pub use selected::{open, Connection, Identity};
// Real-SDK fault injection uses the selected OS entropy source privately.
#[cfg(all(test, hal_enabled))]
pub(crate) use selected::Source;

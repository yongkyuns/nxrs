//! Admission and OS acquisition belong at this boundary.
#[cfg_attr(all(hal_enabled, target_os = "nuttx"), path = "nuttx/mod.rs")]
#[cfg_attr(not(hal_enabled), path = "unbound.rs")]
mod selected;
pub use selected::adopt;

//! Source admission stays at the factory; SDK behavior has no OS branches.
#[cfg(all(hal_enabled, target_os = "linux"))]
mod linux;
#[cfg(all(hal_enabled, target_os = "linux"))]
use linux as selected;
#[cfg(all(hal_enabled, target_os = "macos"))]
mod macos;
#[cfg(all(hal_enabled, target_os = "macos"))]
use macos as selected;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
mod nuttx;
#[cfg(hal_enabled)]
pub use crate::sdk::{Connection, Identity};
#[cfg(all(hal_enabled, target_os = "nuttx"))]
use nuttx as selected;
#[cfg(not(hal_enabled))]
mod unbound;
pub use selected::open;
#[cfg(all(test, hal_enabled))]
pub(crate) use selected::Source;
#[cfg(not(hal_enabled))]
pub use selected::{Connection, Identity};
#[cfg(not(hal_enabled))]
use unbound as selected;

//! Source admission stays at the factory; SDK behavior has no OS branches.
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
use linux as selected;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "macos")]
use macos as selected;
#[cfg(target_os = "nuttx")]
mod nuttx;
#[cfg(target_os = "nuttx")]
use nuttx as selected;
pub use selected::open;
#[cfg(test)]
pub(crate) use selected::Source;

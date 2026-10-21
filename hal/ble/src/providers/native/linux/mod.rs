//! Platform Rust implementation and its private SDK glue.
use super::{Report, ScanBackend};
#[cfg(not(test))]
pub(crate) mod gatt;
mod gio;
mod identity;
mod scan;

pub(super) use scan::State;

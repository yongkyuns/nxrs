//! Platform Rust implementation and its private SDK glue.
use crate::shared::{Report, ScanBackend};
#[cfg(not(test))]
pub(crate) mod gatt;
mod scan;
mod uuid;

pub(super) use scan::State;

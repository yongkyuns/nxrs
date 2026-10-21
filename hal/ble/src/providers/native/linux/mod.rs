//! Platform Rust implementation and its private SDK glue.
use super::Row;
#[cfg(not(test))]
mod gatt;
mod gio;
mod identity;
mod scan;

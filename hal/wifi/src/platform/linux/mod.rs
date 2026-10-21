//! Platform Rust implementation and its private SDK glue.
use super::standard::Station;
mod gio;
mod station;

pub(super) use station::State;

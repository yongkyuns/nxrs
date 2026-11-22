//! Telemetry delivery for packet and stream outputs.
#![cfg_attr(not(feature = "stream"), no_std)]
#![forbid(unsafe_code)]

#[cfg(feature = "summary")]
mod summary;
#[cfg(feature = "summary")]
pub use summary::{
    payload_checksum, Error, Telemetry, TelemetryService, TelemetryStats, SUMMARY_BYTES,
};

#[cfg(feature = "stream")]
pub mod stream;

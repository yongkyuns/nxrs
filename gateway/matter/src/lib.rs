//! Gateway Matter ownership and persistence. No firmware or capture dependencies.
#![forbid(unsafe_code)]

pub mod commissioning;
pub mod config;
mod credential;
pub mod media;
pub mod occupancy;
pub mod persistence;
mod platform;
#[cfg(any(target_os = "linux", target_os = "macos"))]
pub use platform::lan;
pub mod privacy;
pub mod snapshot;

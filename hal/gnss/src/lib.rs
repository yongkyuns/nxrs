//! GNSS contract and capability-local acquisition.
#![cfg_attr(target_os = "none", no_std)]
#![deny(unsafe_code)]
#[cfg(not(target_os = "none"))]
#[cfg_attr(not(any(test, platform = "nuttx")), allow(dead_code))]
pub mod aiding;
mod api;
#[cfg(not(target_os = "none"))]
pub use acquisition::monotonic_us;
#[cfg(not(target_os = "none"))]
pub use aiding::AidingHandle;
pub use api::{
    Constellation, Device, DeviceError, Event, FixQuality, GnssFix, SatelliteReport,
    SatelliteSignal, SatelliteStatus, Session,
};
pub use messaging::{DeliveryError, Rejected};
#[cfg(not(platform = "nuttx"))]
mod fixtures;
mod platform;
#[cfg(any(test, platform = "nuttx"))]
mod pps;
#[cfg(any(test, platform = "nuttx"))]
mod receiver;
#[cfg(any(test, platform = "mock", platform = "nuttx", platform = "simulation"))]
mod session;
/// Construction is reserved for platform catalogues and qualification.
pub use platform::construction;

#[cfg(test)]
mod session_tests;
#[cfg(test)]
mod test_support;
#[cfg(test)]
use test_support::mock_tests;

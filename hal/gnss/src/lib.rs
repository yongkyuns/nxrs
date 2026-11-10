//! GNSS contract and capability-local acquisition.
#![cfg_attr(target_os = "none", no_std)]
#![deny(unsafe_code)]
mod api;
pub use api::{Device, DeviceError, Event, GnssFix, Session};
pub use messaging::{DeliveryError, Rejected};
#[cfg(not(platform = "nuttx"))]
mod fixtures;
mod platform;
#[cfg(any(test, platform = "nuttx"))]
mod receiver;
#[cfg(any(test, platform = "nuttx", platform = "simulation"))]
mod session;
/// Construction is reserved for platform catalogues and qualification.
pub use platform::construction;

#[cfg(test)]
mod session_tests;
#[cfg(test)]
mod test_support;
#[cfg(test)]
use test_support::mock_tests;

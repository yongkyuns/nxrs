//! GNSS contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(
    test,
    hal_provider = "nuttx",
    hal_provider = "mock",
    hal_provider = "simulation"
))]
extern crate std;
mod api;
/// Compatibility name for the acquisition session; use `Session` in new code.
pub use api::Session as Gnss;
pub use api::{DeviceError, Event, GnssFix, Session};
pub use messaging::{DeliveryError, Rejected};
mod platform;
#[cfg(any(test, hal_provider = "nuttx"))]
mod receiver;
#[cfg(any(hal_provider = "nuttx", hal_provider = "simulation"))]
mod session;
pub use platform::open;

#[cfg(test)]
mod session_tests;
#[cfg(test)]
mod test_support;
#[cfg(test)]
use test_support::mock_tests;

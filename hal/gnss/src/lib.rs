//! GNSS contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(
    test,
    hal_provider = "hardware",
    hal_provider = "mock",
    hal_provider = "simulation"
))]
extern crate std;
mod api;
pub use api::{DeviceError, Event, Gnss, GnssFix};
pub use messaging::{DeliveryError, Rejected};
mod platform;
#[cfg(any(test, hal_provider = "hardware"))]
mod receiver;
#[cfg(any(hal_provider = "hardware", hal_provider = "simulation"))]
mod session;
pub use platform::open;

#[cfg(test)]
mod session_tests;
#[cfg(test)]
mod test_support;
#[cfg(test)]
use test_support::mock_tests;

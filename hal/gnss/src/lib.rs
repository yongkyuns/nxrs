//! GNSS contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(test, hal_enabled, hal_mock))]
extern crate std;
mod api;
pub use api::{DeviceError, Event, Gnss, GnssFix};
pub use messaging::{DeliveryError, Rejected};
#[cfg(any(test, hal_enabled, hal_mock))]
#[path = "../../../driver/acquisition.rs"]
mod acquisition;
#[cfg(any(test, hal_mock))]
#[path = "../../../driver/mock_acquisition.rs"]
mod mock_acquisition;
mod platform;
#[cfg(any(test, hal_enabled))]
mod receiver;
pub use platform::*;

#[cfg(test)]
mod session_tests;

#[cfg(all(test, not(hal_mock)))]
#[allow(dead_code, unused_imports)]
#[path = "platform/mock.rs"]
mod mock_tests;
#[cfg(all(test, hal_mock))]
use platform as mock_tests;

//! IMU contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(test, hal_enabled, hal_mock))]
extern crate std;
mod api;
pub use api::{DeviceError, Event, Imu, ImuSample};
pub use messaging::{DeliveryError, Rejected};
#[cfg(any(test, hal_enabled))]
mod bmi270;
#[cfg(any(test, hal_enabled))]
mod cadence;
mod platform;
pub use platform::open;

#[cfg(test)]
mod test_support;

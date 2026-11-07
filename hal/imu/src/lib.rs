//! IMU contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(test, hal_enabled, hal_mock))]
extern crate std;
mod api;
pub use api::{DeviceError, Event, Imu, ImuSample};
pub use messaging::{DeliveryError, Rejected};
#[cfg(any(test, hal_enabled))]
mod cadence;
mod platform;
#[cfg(any(test, hal_enabled))]
// Host contract tests do not select the NuttX sensor constructor.
#[cfg_attr(test, allow(dead_code))]
mod sensors;
pub use platform::open;

#[cfg(test)]
mod test_support;

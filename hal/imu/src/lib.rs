//! IMU contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(test, hal_enabled, hal_mock))]
extern crate std;
mod api;
pub use api::{DeviceError, Event, Imu, ImuSample};
pub use messaging::{DeliveryError, Rejected};
#[cfg(any(test, hal_enabled, hal_mock))]
#[path = "../../../driver/acquisition.rs"]
mod acquisition;
#[cfg(any(test, hal_enabled))]
mod bmi270;
#[cfg(any(test, hal_enabled))]
mod cadence;
#[cfg(any(test, hal_mock))]
#[path = "../../../driver/mock_acquisition.rs"]
mod mock_acquisition;
mod platform;
pub use platform::*;

#[cfg(all(test, unix))]
#[path = "platform/nuttx/irq_tests.rs"]
mod irq_tests;

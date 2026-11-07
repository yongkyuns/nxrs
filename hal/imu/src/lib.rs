//! IMU contract and capability-local acquisition.
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
pub use api::{
    AccelRange, AppliedConfig, Capabilities, Config, DeviceError, Event, GyroRange, Imu, ImuSample,
};
pub use messaging::{DeliveryError, Rejected};
#[cfg(any(test, hal_provider = "hardware"))]
mod cadence;
mod platform;
#[cfg(any(test, hal_provider = "hardware"))]
// Host contract tests do not select the NuttX sensor constructor.
#[cfg_attr(test, allow(dead_code))]
mod sensors;
#[cfg(any(
    all(test, unix),
    hal_provider = "hardware",
    hal_provider = "simulation"
))]
mod session;
pub use platform::{capabilities, open, open_configured};

#[cfg(test)]
mod test_support;

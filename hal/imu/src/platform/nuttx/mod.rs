//! Generic NuttX IMU acquisition over `peripheral::imu::Imu`.
#[cfg(not(test))]
use super::{events, gpio_resource};
use crate::device_state;
#[cfg(test)]
use test_support::{ffi as events, gpio_resource};
mod device;
mod factory;
mod instance;
mod signal;
mod trace;

pub use instance::Instance;
pub use trace::{Observed, Trace};

#[cfg(test)]
mod instance_tests;
#[cfg(test)]
#[path = "../../../tests/support/mod.rs"]
mod test_support;

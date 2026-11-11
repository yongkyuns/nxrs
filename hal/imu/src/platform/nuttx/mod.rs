//! Generic NuttX IMU acquisition over `peripheral::imu::Imu`.
use crate::device_state;
#[cfg(test)]
use test_support::{ffi as events, gpio_resource};
#[cfg(not(test))]
use {::gpio as gpio_resource, acquisition::nuttx as events};
mod device;
mod instance;
mod signal;

pub use instance::Instance;

#[cfg(test)]
mod instance_tests;
#[cfg(test)]
#[path = "../../../tests/support/mod.rs"]
mod test_support;

//! Generic NuttX IMU acquisition over `peripheral::imu::Imu`.
use super::{events, gpio_resource};
use crate::device_state;
mod device;
mod factory;
mod instance;
mod signal;
mod trace;

pub use instance::Instance;
pub use trace::{Observed, Trace};

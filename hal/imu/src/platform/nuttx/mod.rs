//! Generic NuttX IMU acquisition over `peripheral::imu::Imu`.
use super::{events, gpio_resource};
mod device;
#[path = "../../device_state.rs"]
mod device_state;
mod factory;
mod instance;
mod signal;
mod trace;

pub use instance::Instance;
pub use trace::{Observed, Trace};

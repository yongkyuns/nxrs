//! Profile-selected IMU devices. Applications choose names; services receive
//! normalized `imu::Device` references. Acquisition remains in the IMU HAL.
#![forbid(unsafe_code)]

#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
use {::i2c as resource, ::spi as spi_resource};

#[cfg(not(all(hal_provider = "hardware", target_os = "nuttx")))]
mod fixtures;
#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
mod nuttx;

#[cfg(not(all(hal_provider = "hardware", target_os = "nuttx")))]
use fixtures as selected;
#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
use nuttx as selected;

// The generated device names are this catalogue’s public API.
pub use selected::devices::*;
pub mod probe;

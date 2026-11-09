//! Profile-selected IMU devices. Applications choose names; services receive
//! normalized `imu::Device` references. Acquisition remains in the IMU HAL.
#![forbid(unsafe_code)]

#[cfg(all(hal_provider = "hardware", target_os = "nuttx"))]
use {::i2c as resource, ::spi as spi_resource};

#[cfg_attr(
    all(hal_provider = "hardware", target_os = "nuttx"),
    path = "nuttx/mod.rs"
)]
#[cfg_attr(
    not(all(hal_provider = "hardware", target_os = "nuttx")),
    path = "fixtures.rs"
)]
mod selected;

// The generated device names are this catalogue’s public API.
pub use selected::devices::*;
pub mod probe;

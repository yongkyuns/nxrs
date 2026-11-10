//! Profile-selected IMU devices. Applications choose names; services receive
//! normalized `imu::Device` references. Acquisition remains in the IMU HAL.
#![forbid(unsafe_code)]

cfg_if::cfg_if! {
    if #[cfg(hal_provider = "nuttx")] {
        use {::i2c as resource, ::spi as spi_resource};
        mod nuttx;
        use nuttx as selected;
    } else {
        mod fixtures;
        use fixtures as selected;
    }
}

// The generated device names are this catalogue’s public API.
pub use selected::devices::*;
pub mod probe;

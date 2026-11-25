//! Concrete resource inputs for profile construction; no acquisition policy.
use super::{DeviceError, Observed, Trace};

#[path = "imu_resources/delay.rs"]
mod delay;
pub(super) use delay::Delay;

cfg_if::cfg_if! {
    if #[cfg(feature = "i2c")] {
        #[path = "imu_resources/i2c.rs"]
        mod i2c;
        pub(super) use i2c::{Bus, Resource as I2cResource, open as open_i2c};
    }
}
cfg_if::cfg_if! {
    if #[cfg(feature = "spi")] {
        #[path = "imu_resources/spi.rs"]
        mod spi;
        pub(super) use spi::{Device as Spi, Resource as SpiResource, open as open_spi};
    }
}

//! Concrete resource inputs for profile construction.
use embedded_hal::delay::DelayNs;
use std::time::Duration;

cfg_if::cfg_if! {
    if #[cfg(feature = "i2c")] {
        mod i2c;
        pub(super) use i2c::{Bus, I2cBinding, I2cResource};
    }
}
cfg_if::cfg_if! {
    if #[cfg(feature = "spi")] {
        mod spi;
        pub(super) use spi::{Spi, SpiBinding, SpiResource};
    }
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

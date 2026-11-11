//! Concrete resource inputs for profile construction; no acquisition policy.
use super::{resource, spi_resource, DeviceError, Observed, Trace};
use embedded_hal::delay::DelayNs;
use std::time::Duration;

pub(super) type Bus = Observed<resource::Bus>;
pub(super) type Spi = Observed<spi_resource::Device>;

pub(super) fn open_i2c(
    (config, address): (resource::Config<'_>, u8),
    trace: Trace,
) -> Result<I2cResource, DeviceError> {
    resource::Bus::open(config)
        .map(|bus| (trace.observe(bus), address))
        .map_err(|_| DeviceError::Io)
}

pub(super) fn open_spi(
    config: spi_resource::Config<'_>,
    trace: Trace,
) -> Result<SpiResource, DeviceError> {
    spi_resource::Device::open(config)
        .map(|device| trace.observe(device))
        .map_err(|_| DeviceError::Io)
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) type I2cResource = (Bus, u8);
pub(super) type SpiResource = Spi;

//! Concrete resource inputs for profile construction; no acquisition policy.
use super::{resource, spi_resource, DeviceError, Observed, Trace};
use embedded_hal::delay::DelayNs;
use std::time::Duration;

pub(super) type Bus = Observed<resource::Bus>;
pub(super) type Spi = Observed<spi_resource::Device>;

pub(super) struct I2cBinding<'a> {
    pub(super) path: &'a str,
    pub(super) address: u8,
    pub(super) frequency: u32,
    pub(super) irq_path: Option<&'a str>,
}

impl I2cBinding<'_> {
    pub(super) fn open(&self, trace: Trace) -> Result<(Bus, u8), DeviceError> {
        let bus = resource::Bus::open(resource::Config {
            path: self.path,
            frequency: self.frequency,
        })
        .map_err(|_| DeviceError::Io)?;
        Ok((trace.observe(bus), self.address))
    }
}
pub(super) struct SpiBinding<'a> {
    pub(super) path: &'a str,
    pub(super) chip_select: u16,
    pub(super) frequency: u32,
    pub(super) mode: u8,
    pub(super) irq_path: Option<&'a str>,
}
impl SpiBinding<'_> {
    pub(super) fn open(&self, trace: Trace) -> Result<Spi, DeviceError> {
        spi_resource::Device::open(spi_resource::Config {
            path: self.path,
            chip_select: self.chip_select,
            frequency: self.frequency,
            mode: self.mode,
        })
        .map(|device| trace.observe(device))
        .map_err(|_| DeviceError::Io)
    }
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) type I2cResource = (Bus, u8);
pub(super) type SpiResource = Spi;

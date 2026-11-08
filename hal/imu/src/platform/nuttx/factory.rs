//! Acquire profile resources, then construct a device using the supplied factory.
use super::{
    device::{self, Device, Lease},
    observed_bus::{Bus, Spi},
    signal::Signal,
    spi_resource,
    trace::Trace,
};
use crate::{Config, DeviceError};
use embedded_hal::delay::DelayNs;
use peripheral::imu::{Error, Imu};
use std::time::Duration;

pub(super) struct I2cBinding<'a> {
    pub(super) path: &'a str,
    pub(super) address: u8,
    pub(super) frequency: u32,
    pub(super) irq_path: Option<&'a str>,
}

impl I2cBinding<'_> {
    pub(super) fn open(&self, trace: Trace) -> Result<(Bus, u8), DeviceError> {
        Ok((Bus::open(self.path, self.frequency, trace)?, self.address))
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
        Spi::open(
            spi_resource::Config {
                path: self.path,
                chip_select: self.chip_select,
                frequency: self.frequency,
                mode: self.mode,
            },
            trace,
        )
    }
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) fn acquire<S: Imu, R>(
    config: Config,
    irq_path: Option<&str>,
    trace: Trace,
    open: impl FnOnce() -> Result<R, DeviceError>,
    construct: impl FnOnce(R, S::Settings) -> Result<S, Error>,
) -> Result<Device<S>, DeviceError> {
    let (settings, applied) = device::resolve::<S>(config)?;
    let lease = Lease::take()?;
    // A configured IRQ failure must never select the periodic mode.
    let irq = irq_path.map(device::open_irq).transpose()?;
    let bus = open()?;
    let sensor = construct(bus, settings).inspect_err(|_| {
        // An interrupted configuration may have left the sensor active.
        device::quarantine();
    })?;
    let period = Duration::from_micros(u64::from(applied.delivery_interval_us));
    Ok(Device::new(
        sensor,
        applied,
        Signal::new(irq, period),
        trace,
        lease,
    ))
}

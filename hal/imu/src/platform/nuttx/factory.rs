//! Acquire profile resources, then construct a device using the supplied factory.
use super::device_state::DeviceState;
use super::{
    device::{self, Device},
    resource,
    signal::Signal,
    spi_resource,
    trace::{Observed, Trace},
};
use crate::{Config, DeviceError};
use embedded_hal::delay::DelayNs;
use peripheral::imu::{Error, Imu};
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

pub(super) fn acquire<S: Imu, R>(
    config: Config,
    irq_path: Option<&str>,
    trace: Trace,
    state: &'static DeviceState,
    open: impl FnOnce() -> Result<R, DeviceError>,
    construct: impl FnOnce(R, S::Settings) -> Result<S, Error>,
) -> Result<Device<S>, DeviceError> {
    let (settings, applied) = device::resolve::<S>(config)?;
    let use_guard = state.begin_use()?;
    // A configured IRQ failure must never select the periodic mode.
    let irq = irq_path
        .map(|path| device::open_irq(path, state))
        .transpose()?;
    let bus = open()?;
    let sensor = construct(bus, settings).inspect_err(|_| {
        // An interrupted configuration may have left the sensor active.
        state.mark_faulted();
    })?;
    let period = Duration::from_micros(u64::from(applied.delivery_interval_us));
    Ok(Device::new(
        sensor,
        applied,
        Signal::new(irq, period),
        trace,
        use_guard,
    ))
}

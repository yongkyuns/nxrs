//! Acquire profile resources, then construct a device using the supplied factory.
use super::{
    device::{self, Device, Lease},
    observed_bus::Bus,
    signal::Signal,
    trace::Trace,
};
use crate::{Config, DeviceError};
use embedded_hal::delay::DelayNs;
use peripheral::imu::{Error, Imu};
use std::time::Duration;

pub(super) struct Binding<'a> {
    pub(super) path: &'a str,
    pub(super) address: u8,
    pub(super) frequency: u32,
    pub(super) irq_path: Option<&'a str>,
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) fn acquire<S: Imu>(
    binding: Binding<'_>,
    config: Config,
    trace: Trace,
    construct: impl FnOnce(Bus, u8, S::Settings) -> Result<S, Error>,
) -> Result<Device<S>, DeviceError> {
    let (settings, applied) = device::resolve::<S>(config)?;
    let lease = Lease::take()?;
    // A configured IRQ failure must never select the periodic mode.
    let irq = binding.irq_path.map(device::open_irq).transpose()?;
    let bus = Bus::open(binding.path, binding.frequency, trace.clone())?;
    let sensor = construct(bus, binding.address, settings).inspect_err(|_| {
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

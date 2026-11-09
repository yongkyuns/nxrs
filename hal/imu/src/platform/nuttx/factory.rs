//! Validate and acquire resources before invoking a normalized sensor constructor.
use super::{
    device::{self, Device},
    device_state::DeviceState,
    signal::Signal,
    trace::Trace,
};
use crate::{Config, DeviceError};
use peripheral::imu::{Error, Imu};
use std::time::Duration;

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

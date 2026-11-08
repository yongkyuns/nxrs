//! Profile-selected NuttX IMU factory.
use self::{construction::Construct, session::Session};
use super::{events, gpio_resource, resource};
use crate::{AppliedConfig, Config, DeviceError, Event, Imu};
use acquisition::readiness;
use core::sync::atomic::AtomicU8;
use device::{Binding, Delay, Device};
use observed_bus::Bus;
use peripheral::imu::Imu as ImuDevice;
use trace::Trace;

#[path = "../../cadence.rs"]
mod cadence;
#[path = "../../session.rs"]
mod session;

mod construction;
mod device;
mod observed_bus;
mod signal;
mod trace;

static OWNED: AtomicU8 = AtomicU8::new(0);
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

// Catalog implementation IDs name these checked-in concrete types.
#[allow(dead_code, reason = "The catalog selects one concrete implementation.")]
mod implementations {
    pub(super) type Bmi270 = nxrs_bmi270::Bmi270<super::Bus, super::Delay>;
    pub(super) type Lsm6dso = nxrs_lsm6dso::Lsm6dso<super::Bus>;
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let config = Implementation::DEFAULT;
    open_configured(config, sink).map(|(session, _)| session)
}

/// Preview accepted settings without opening hardware or reserving the device.
pub fn resolve(config: Config) -> Result<AppliedConfig, DeviceError> {
    device::resolve::<Implementation>(config).map(|(_, applied)| applied)
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    open_with_trace(config, sink, Trace::default())
}

pub(crate) fn open_probed(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(crate::probe::Timing) + Send + Sync + 'static,
    sensor_reference: bool,
) -> Result<(std::boxed::Box<dyn Imu + Send>, AppliedConfig), DeviceError> {
    let trace = Trace::new(observe, sensor_reference)?;
    let (session, applied) = open_with_trace(config, sink, trace)?;
    Ok((std::boxed::Box::new(session), applied))
}

fn open_with_trace(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    trace: Trace,
) -> Result<(Session<readiness::Cancel>, AppliedConfig), DeviceError> {
    acquire::<Implementation>(BINDING, config, sink, trace)
}

fn acquire<T: Construct + Send + 'static>(
    binding: Binding<'_>,
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    trace: Trace,
) -> Result<(Session<readiness::Cancel>, AppliedConfig), DeviceError> {
    let device = Device::<T>::acquire(binding, config, trace)?;
    let applied = device.applied_config();
    let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    Session::start(STACK_BYTES, cancel, sink, move |stop, emit| {
        device.run(stop, &wake, emit)
    })
    .map(|session| (session, applied))
}

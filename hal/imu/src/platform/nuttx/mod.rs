//! Profile-selected NuttX IMU factory.
use crate::{
    sensors::bmi270::{CAPABILITIES, DEFAULT_CONFIG},
    session::Session,
    AppliedConfig, Capabilities, Config, DeviceError, Event, Imu,
};
use acquisition::readiness;
use core::sync::atomic::AtomicU8;
use device::{Binding, Device};

mod device;
#[allow(unsafe_code)]
mod ffi;
mod i2c;
mod irq;
mod signal;

static OWNED: AtomicU8 = AtomicU8::new(0);
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    open_configured(DEFAULT_CONFIG, sink).map(|(session, _)| session)
}

pub fn capabilities() -> Option<Capabilities> {
    Some(CAPABILITIES)
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    let device = match MODEL {
        SensorModel::Bmi270 => Device::acquire(BINDING, config)?,
    };
    let applied = device.applied_config();
    let cancel = readiness::Cancel::new(ffi::event_pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    Session::start(STACK_BYTES, cancel, sink, move |stop, emit| {
        device.run(stop, &wake, emit)
    })
    .map(|session| (session, applied))
}

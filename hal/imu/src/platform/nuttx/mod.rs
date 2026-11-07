//! Profile-selected NuttX IMU factory.
use crate::{
    sensors::{bmi270, lsm6dso, Sensor},
    session::Session,
    AppliedConfig, Capabilities, Config, DeviceError, Event, Imu,
};
use acquisition::readiness;
use core::sync::atomic::AtomicU8;
use device::{Binding, Delay, Device};
use i2c::Bus;

mod device;
#[allow(unsafe_code)]
mod ffi;
mod i2c;
mod irq;
mod signal;

static OWNED: AtomicU8 = AtomicU8::new(0);
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

type Bmi270 = bmi270::Sensor<Bus, Delay>;
type Lsm6dso = lsm6dso::Sensor<Bus>;

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let config = match MODEL {
        SensorModel::Bmi270 => Bmi270::DEFAULT,
        SensorModel::Lsm6dso => <Lsm6dso as Sensor<Bus, Delay>>::DEFAULT,
    };
    open_configured(config, sink).map(|(session, _)| session)
}

pub fn capabilities() -> Option<Capabilities> {
    Some(match MODEL {
        SensorModel::Bmi270 => Bmi270::CAPABILITIES,
        SensorModel::Lsm6dso => <Lsm6dso as Sensor<Bus, Delay>>::CAPABILITIES,
    })
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    match MODEL {
        SensorModel::Bmi270 => acquire::<Bmi270>(BINDING, config, sink),
        SensorModel::Lsm6dso => acquire::<Lsm6dso>(BINDING, config, sink),
    }
}

fn acquire<T: Sensor<Bus, Delay> + Send + 'static>(
    binding: Binding<'_>,
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
) -> Result<(Session<readiness::Cancel>, AppliedConfig), DeviceError> {
    let device = Device::<T>::acquire(binding, config)?;
    let applied = device.applied_config();
    let cancel = readiness::Cancel::new(ffi::event_pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    Session::start(STACK_BYTES, cancel, sink, move |stop, emit| {
        device.run(stop, &wake, emit)
    })
    .map(|session| (session, applied))
}

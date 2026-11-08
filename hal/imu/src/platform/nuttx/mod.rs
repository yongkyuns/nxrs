//! Profile-selected NuttX IMU factory.
use self::session::WorkerSession;
use super::{events, gpio_resource, resource, spi_resource};
use crate::{AppliedConfig, Config, DeviceError, Event, Session};
use acquisition::readiness;
use core::sync::atomic::AtomicU8;
use device::Device;
use factory::Delay;
use observed_bus::{Bus, Spi};
use peripheral::imu::Imu as ImuDevice;
use trace::Trace;

#[path = "../../cadence.rs"]
mod cadence;
#[path = "../../session.rs"]
mod session;

mod device;
#[allow(dead_code, reason = "The profile selects one resource constructor.")]
mod factory;
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
    pub(super) type Bmi270Spi = nxrs_bmi270::Bmi270Spi<super::Spi, super::Delay>;
    pub(super) type Lsm6dsoSpi = nxrs_lsm6dso::Lsm6dsoSpi<super::Spi>;
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Session + Send + 'static, DeviceError> {
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
) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
    start_device(acquire_device(config, Trace::default())?, sink)
}

pub(crate) fn open_probed(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(crate::probe::Timing) + Send + Sync + 'static,
    sensor_reference: bool,
) -> Result<(std::boxed::Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    let trace = Trace::new(observe)?;
    let mut device = acquire_device(config, trace)?;
    if sensor_reference {
        device = device.prepare(|sensor| sensor.enable_sensor_reference().map_err(Into::into))?;
    }
    let (session, applied) = start_device(device, sink)?;
    Ok((std::boxed::Box::new(session), applied))
}

fn acquire_device(config: Config, trace: Trace) -> Result<Device<Implementation>, DeviceError> {
    let observed = trace.clone();
    factory::acquire(
        config,
        BINDING.irq_path,
        trace,
        || BINDING.open(observed),
        |bus, settings| {
            let interface = bus
                .try_into()
                .map_err(|_| peripheral::imu::Error::InvalidData)?;
            Implementation::from_interface(interface, Delay, settings).map_err(Into::into)
        },
    )
}

fn start_device<T: ImuDevice + Send + 'static>(
    device: Device<T>,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
) -> Result<(WorkerSession<readiness::Cancel>, AppliedConfig), DeviceError> {
    let applied = device.applied_config();
    let cancel = readiness::Cancel::new(events::pipe).map_err(|_| DeviceError::Io)?;
    let wake = cancel.reader.clone();
    WorkerSession::start(STACK_BYTES, cancel, sink, move |stop, emit| {
        device.run(stop, &wake, emit)
    })
    .map(|session| (session, applied))
}

//! Concrete driver/resource wiring from the validated profile.
#[cfg(feature = "i2c")]
use super::resource;
#[cfg(feature = "spi")]
use super::spi_resource;
use ::imu::construction::Instance;
#[cfg(feature = "probe")]
use ::imu::{AppliedConfig, Config, Event, Session};
use ::imu::{Device, DeviceError};
use acquisition::probe::{Observed, Trace};
#[allow(dead_code, reason = "Profiles select the resource constructors.")]
#[path = "imu_resources.rs"]
mod resources;
// Resource opening and sensor initialization are separate callbacks so the HAL
// can distinguish a missing endpoint from an uncertain sensor configuration.
// Optional sensor diagnostics belong to this typed factory, not the generator.
macro_rules! declare_sensor_reference {
    (PRIMARY_DEVICE, $sensor:ty, true) => {
        #[cfg(feature = "probe")]
        const SENSOR_REFERENCE: Option<fn(&mut $sensor) -> Result<(), peripheral::imu::Error>> =
            Some(|sensor| sensor.enable_sensor_reference().map_err(Into::into));
    };
    (PRIMARY_DEVICE, $sensor:ty, false) => {
        #[cfg(feature = "probe")]
        const SENSOR_REFERENCE: Option<fn(&mut $sensor) -> Result<(), peripheral::imu::Error>> =
            None;
    };
    ($name:ident, $sensor:ty, $reference:tt) => {};
}
macro_rules! declare_device {
    ($name:ident, $id:literal, $sensor:ty, $resources:ty, $open:path, $binding:expr, $irq:expr, $stack:expr, $reference:tt) => {
        static $name: Instance<$sensor, $resources> = Instance::new(
            $id,
            $irq,
            $stack,
            |trace| $open($binding, trace),
            |bus, settings| {
                let interface = bus
                    .try_into()
                    .map_err(|_| peripheral::imu::Error::InvalidData)?;
                <$sensor>::from_interface(interface, resources::Delay, settings).map_err(Into::into)
            },
        );
        declare_sensor_reference!($name, $sensor, $reference);
    };
}
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

#[cfg(feature = "probe")]
pub(crate) fn open_probed(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(acquisition::probe::Timing) + Send + Sync + 'static,
    sensor_reference: bool,
) -> Result<(std::boxed::Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    let reference = if sensor_reference {
        Some(SENSOR_REFERENCE.ok_or(DeviceError::Unsupported)?)
    } else {
        None
    };
    PRIMARY_DEVICE
        .start(
            config,
            Trace::new(observe).ok_or(DeviceError::Unsupported)?,
            |sensor| reference.map_or(Ok(()), |enable| enable(sensor)),
            sink,
        )
        .map(|(session, applied)| {
            (
                std::boxed::Box::new(session) as std::boxed::Box<dyn Session + Send>,
                applied,
            )
        })
}

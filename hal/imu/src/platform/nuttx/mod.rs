//! Profile-selected canonical IMU instances and concrete resource construction.
use super::{events, gpio_resource, resource, spi_resource};
use crate::{AppliedConfig, Config, DeviceError, Event, Session};
use factory::{Bus, Delay, Spi};
use trace::Trace;
#[path = "../../device_state.rs"]
mod device_state;

mod device;
#[allow(dead_code, reason = "Profiles select the resource constructors.")]
mod factory;
mod instance;
mod signal;
mod trace;

#[allow(dead_code, reason = "Profiles select concrete implementations.")]
mod implementations {
    pub(super) type Bmi270 = nxrs_bmi270::Bmi270<super::Bus, super::Delay>;
    pub(super) type Lsm6dso = nxrs_lsm6dso::Lsm6dso<super::Bus>;
    pub(super) type Bmi270Spi = nxrs_bmi270::Bmi270Spi<super::Spi, super::Delay>;
    pub(super) type Lsm6dsoSpi = nxrs_lsm6dso::Lsm6dsoSpi<super::Spi>;
}

// Generated input supplies only checked implementation IDs and typed values.
// One factory call at acquisition; no model/bus dispatch in the sample loop.
macro_rules! declare_device {
    ($name:ident, $id:literal, $sensor:ty, $binding_type:ty, $binding:expr, $stack:expr) => {
        #[allow(
            dead_code,
            reason = "Profiles may declare devices without a consuming role."
        )]
        static $name: instance::Instance<$sensor, $binding_type> = instance::Instance::new(
            $id,
            $binding,
            $stack,
            |binding, config, trace, state, reference| {
                let observed = trace.clone();
                let device = factory::acquire(
                    config,
                    binding.irq_path,
                    trace,
                    state,
                    || binding.open(observed),
                    |bus, settings| {
                        let interface = bus
                            .try_into()
                            .map_err(|_| peripheral::imu::Error::InvalidData)?;
                        <$sensor>::from_interface(interface, Delay, settings).map_err(Into::into)
                    },
                )?;
                if reference {
                    device.prepare(|sensor| sensor.enable_sensor_reference().map_err(Into::into))
                } else {
                    Ok(device)
                }
            },
        );
    };
}
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

pub(crate) fn open_probed(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(crate::probe::Timing) + Send + Sync + 'static,
    sensor_reference: bool,
) -> Result<(std::boxed::Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    primary_instance()
        .start(config, Trace::new(observe)?, sensor_reference, sink)
        .map(|(session, applied)| {
            (
                std::boxed::Box::new(session) as std::boxed::Box<dyn Session + Send>,
                applied,
            )
        })
}

//! Concrete driver/resource wiring from the validated profile.
use super::{resource, spi_resource};
use acquisition::probe::{Observed, Trace};
use imu::construction::Instance;
use imu::{AppliedConfig, Config, DeviceError, Event, Session};
#[allow(dead_code, reason = "Profiles select the resource constructors.")]
mod resources;
use resources::{Bus, Delay, Spi};

#[allow(dead_code, reason = "Profiles select concrete implementations.")]
mod implementations {
    cfg_if::cfg_if! {
        if #[cfg(feature = "bmi270")] {
            pub(super) type Bmi270 = nxrs_bmi270::Bmi270<super::Bus, super::Delay>;
            pub(super) type Bmi270Spi = nxrs_bmi270::Bmi270Spi<super::Spi, super::Delay>;
        }
    }
    cfg_if::cfg_if! {
        if #[cfg(feature = "lsm6dso")] {
            pub(super) type Lsm6dso = nxrs_lsm6dso::Lsm6dso<super::Bus>;
            pub(super) type Lsm6dsoSpi = nxrs_lsm6dso::Lsm6dsoSpi<super::Spi>;
        }
    }
}

// Resource opening and sensor initialization are separate callbacks so the HAL
// can distinguish a missing endpoint from an uncertain sensor configuration.
macro_rules! declare_device {
    ($name:ident, $id:literal, $sensor:ty, $binding_type:ty, $resources:ty, $binding:expr, $stack:expr) => {
        static $name: Instance<$sensor, $resources> = Instance::new(
            $id,
            ($binding).irq_path,
            $stack,
            |trace| {
                let binding: $binding_type = $binding;
                binding.open(trace)
            },
            |bus, settings| {
                let interface = bus
                    .try_into()
                    .map_err(|_| peripheral::imu::Error::InvalidData)?;
                <$sensor>::from_interface(interface, Delay, settings).map_err(Into::into)
            },
        );
    };
}
include!(concat!(env!("OUT_DIR"), "/imu_config.rs"));

pub(crate) fn open_probed(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(acquisition::probe::Timing) + Send + Sync + 'static,
    sensor_reference: bool,
) -> Result<(std::boxed::Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    primary_instance()
        .start(
            config,
            Trace::new(observe).ok_or(DeviceError::Unsupported)?,
            |sensor| {
                if sensor_reference {
                    sensor.enable_sensor_reference().map_err(Into::into)
                } else {
                    Ok(())
                }
            },
            sink,
        )
        .map(|(session, applied)| {
            (
                std::boxed::Box::new(session) as std::boxed::Box<dyn Session + Send>,
                applied,
            )
        })
}

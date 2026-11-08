//! Translate chip-specific driver APIs into the IMU contract for shared acquisition.
use crate::{AppliedConfig, Config, DeviceError, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};

#[path = "../configuration.rs"]
mod configuration;

pub(crate) mod bmi270;
pub(crate) mod lsm6dso;

/// Synchronous operations required by the common task-context acquisition path.
pub(crate) trait Sensor<B: I2c, D: DelayNs>: Sized {
    const DEFAULT: Config;
    type Settings;

    fn resolve(config: Config) -> Result<(Self::Settings, AppliedConfig), DeviceError>;

    fn open(
        bus: B,
        address: u8,
        delay: D,
        settings: Self::Settings,
        config: AppliedConfig,
    ) -> Result<Self, DeviceError>;
    fn enable_sensor_reference(&mut self) -> Result<(), DeviceError>;
    fn applied_config(&self) -> AppliedConfig;
    fn drain(
        &mut self,
        reception_ms: u64,
        emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError>;
    fn stop(&mut self) -> Result<(), DeviceError>;
}

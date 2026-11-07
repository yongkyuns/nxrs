use crate::{AppliedConfig, Capabilities, Config, DeviceError, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};

pub(crate) mod bmi270;
pub(crate) mod lsm6dso;

/// Synchronous operations required by the common task-context acquisition path.
pub(crate) trait Sensor<B: I2c, D: DelayNs>: Sized {
    const DEFAULT: Config;
    const CAPABILITIES: Capabilities;

    fn open(bus: B, address: u8, delay: D, config: AppliedConfig) -> Result<Self, DeviceError>;
    fn applied_config(&self) -> AppliedConfig;
    fn drain(
        &mut self,
        reception_ms: u64,
        emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError>;
    fn stop(&mut self) -> Result<(), DeviceError>;
}

//! Lifecycle witnesses explicitly include both production resource adapters.
use super::{DeviceError, Observed, Trace};

#[path = "../../../../platform/src/nuttx/imu_resources/delay.rs"]
mod delay;
#[path = "../../../../platform/src/nuttx/imu_resources/i2c.rs"]
mod i2c;
#[path = "../../../../platform/src/nuttx/imu_resources/spi.rs"]
mod spi;

pub(super) use delay::Delay;
pub(super) use i2c::{open as open_i2c, Bus};
pub(super) use spi::{open as open_spi, Device as Spi};

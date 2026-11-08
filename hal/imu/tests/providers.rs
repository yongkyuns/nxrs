//! Host-injected witnesses are separate from production module selection.
#![deny(unsafe_code)]
pub use imu::probe;
pub use imu::{AppliedConfig, Config, DeviceError, Event, ImuSample, Session};
#[path = "../src/cadence.rs"]
mod cadence;

#[cfg(unix)]
#[path = "../src/platform/nuttx/irq_tests.rs"]
mod irq_tests;

#[cfg(unix)]
use irq_tests::{ffi, spi_ffi};
#[cfg(unix)]
#[path = "../../../driver/i2c/tests/support/mod.rs"]
mod resource;

#[cfg(unix)]
#[path = "../../../driver/gpio/tests/support/mod.rs"]
mod gpio_resource;

#[cfg(unix)]
#[path = "../../../driver/spi/tests/support/mod.rs"]
mod spi_resource;

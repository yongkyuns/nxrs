//! Host-injected witnesses are separate from production module selection.
#![deny(unsafe_code)]
pub use imu::probe;
pub use imu::{AppliedConfig, Config, DeviceError, Event, Imu, ImuSample};
#[path = "../src/cadence.rs"]
mod cadence;

#[cfg(unix)]
#[path = "../src/platform/nuttx/irq_tests.rs"]
mod irq_tests;

#[cfg(unix)]
use irq_tests::ffi;
#[cfg(unix)]
#[path = "../../../driver/i2c/tests/support/mod.rs"]
mod resource;

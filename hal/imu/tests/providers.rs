//! Host-injected witnesses are separate from production module selection.
#![deny(unsafe_code)]
#![allow(
    clippy::duplicate_mod,
    reason = "Lifecycle witnesses inject distinct readiness backends into the same provider source."
)]
pub use imu::probe;
#[path = "../src/api.rs"]
mod api;
pub use api::{AppliedConfig, Config, Device, DeviceError, Event, ImuSample, Session};
#[path = "../src/device_state.rs"]
mod device_state;
#[path = "../src/session.rs"]
mod session;
#[cfg(unix)]
#[path = "../src/platform/nuttx/trace.rs"]
mod trace;

#[cfg(unix)]
#[path = "../src/platform/nuttx/irq_tests.rs"]
mod irq_tests;

#[cfg(unix)]
#[path = "../src/platform/nuttx/instance_tests.rs"]
mod instance_tests;

#[cfg(unix)]
use irq_tests::{ffi, spi_ffi};
#[cfg(unix)]
#[path = "../../../driver/i2c/tests/support/mod.rs"]
mod resource;

#[cfg(unix)]
#[allow(
    dead_code,
    reason = "The source witness consumes the contextual GPIO constructor."
)]
#[path = "../../../driver/gpio/tests/support/mod.rs"]
mod gpio_resource;

#[cfg(unix)]
#[path = "../../../driver/spi/tests/support/mod.rs"]
mod spi_resource;

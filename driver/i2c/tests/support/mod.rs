use super::ffi;
pub use ::i2c::{Config, Error};
#[path = "../../src/platform/nuttx/bus.rs"]
mod bus;
pub use bus::Bus;

use super::spi_ffi as ffi;
pub use ::spi::{Config, Error};
#[path = "../../src/platform/nuttx/device.rs"]
mod device;
pub use device::Device;

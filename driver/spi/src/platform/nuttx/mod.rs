use crate::{Config, Error};
mod device;
#[allow(unsafe_code)]
mod ffi;
pub use device::Device;

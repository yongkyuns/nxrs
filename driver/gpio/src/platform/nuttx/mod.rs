use crate::Error;
#[allow(unsafe_code)]
mod ffi;
mod interrupt;
pub use interrupt::Interrupt;

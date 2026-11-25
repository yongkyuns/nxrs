use crate::Error;
#[allow(unsafe_code)]
mod ffi;
mod interrupt;
mod pin;
pub use interrupt::Interrupt;
pub use pin::{Input, Output};

use super::ffi;
pub use ::gpio::Error;
#[path = "../../src/platform/nuttx/interrupt.rs"]
mod interrupt;
pub use interrupt::Interrupt;

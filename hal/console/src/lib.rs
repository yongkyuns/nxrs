//! Exclusive byte-stream commissioning console; device selection stays private.
#![no_std]
#![deny(unsafe_code)]
extern crate std;
#[cfg(any(hal_provider = "nuttx", test))]
mod ownership;
mod platform;
pub use platform::open;

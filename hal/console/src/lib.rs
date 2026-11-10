//! Exclusive byte-stream commissioning console; device selection stays private.
#![deny(unsafe_code)]
#[cfg(any(platform = "nuttx", test))]
mod ownership;
mod platform;
pub use platform::open;

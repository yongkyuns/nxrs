//! Owned NuttX SPI device transactions using embedded-hal.
#![deny(unsafe_code)]
mod api;
pub use api::{Config, Error};

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx::Device;

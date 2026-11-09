//! Owned NuttX SPI device transactions using embedded-hal.
#![deny(unsafe_code)]
mod api;
pub use api::{Config, Error};

#[cfg(target_os = "nuttx")]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(target_os = "nuttx")]
pub use nuttx::Device;

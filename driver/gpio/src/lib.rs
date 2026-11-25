//! Owned NuttX digital pins and interrupt registrations.
#![deny(unsafe_code)]
mod api;
pub use api::Error;

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx::{Input, Interrupt, Output};

//! Owned NuttX GPIO interrupt resources. No sensor or profile policy.
#![deny(unsafe_code)]
mod api;
pub use api::Error;

#[cfg(target_os = "nuttx")]
mod platform;
#[cfg(target_os = "nuttx")]
pub use platform::nuttx::Interrupt;

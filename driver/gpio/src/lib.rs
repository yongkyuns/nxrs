//! Owned NuttX GPIO interrupt resources. No sensor or profile policy.
#![deny(unsafe_code)]
mod api;
pub use api::Error;

#[cfg(target_os = "nuttx")]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(target_os = "nuttx")]
pub use nuttx::Interrupt;

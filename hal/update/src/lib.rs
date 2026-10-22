//! Rust-owned firmware staging; activation and protected state are separate gates.
#![no_std]
#![deny(unsafe_code)]

mod api;
pub use api::{Error, Phase, Policy, RequestId, StagedImage, Status, Update};
pub use firmware_image::{Crypto, Layout, Trust, Version};

#[cfg(any(nxrs_hal_platform = "mock", nxrs_hal_platform = "nuttx", test))]
extern crate std;
#[cfg(any(nxrs_hal_platform = "mock", nxrs_hal_platform = "nuttx", test))]
mod controller;
#[cfg(any(nxrs_hal_platform = "mock", nxrs_hal_platform = "nuttx", test))]
pub use controller::{Rejected, Stager};
#[cfg(any(nxrs_hal_platform = "mock", test))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(any(nxrs_hal_platform = "mock", nxrs_hal_platform = "nuttx", test))]
mod protocol;
#[cfg(any(nxrs_hal_platform = "mock", nxrs_hal_platform = "nuttx", test))]
mod worker;
#[cfg(nxrs_hal_platform = "mock")]
pub use mock::open;
#[cfg(nxrs_hal_platform = "nuttx")]
#[allow(unsafe_code)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(nxrs_hal_platform = "nuttx")]
pub use nuttx::open;

#[cfg(test)]
mod tests;

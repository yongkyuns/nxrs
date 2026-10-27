//! Rust-owned firmware staging; activation and protected state are separate gates.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_enabled, hal_mock, test))]
extern crate std;

mod api;
pub use api::{Error, Phase, Policy, RequestId, StagedImage, Status, Update};
pub use firmware_image::{Crypto, Layout, Trust, Version};

#[cfg(any(hal_enabled, hal_mock, test))]
mod controller;
#[cfg(any(hal_enabled, hal_mock, test))]
mod platform;
#[cfg(any(hal_enabled, hal_mock, test))]
mod protocol;
#[cfg(any(hal_enabled, hal_mock, test))]
mod worker;
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::*;

#[cfg(test)]
use platform::mock;
#[cfg(test)]
mod tests;

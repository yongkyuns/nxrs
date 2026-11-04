//! LED contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(hal_enabled, test))]
extern crate std;
mod api;
pub use api::{DeviceError, Led, LedSet};
mod platform;
pub use platform::open;
#[cfg(hal_mock)]
pub use platform::MockLeds;
// Retain the existing qualified native type without importing every backend item.
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use platform::UserLeds;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod test_support;

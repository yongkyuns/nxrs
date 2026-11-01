//! WIFI contracts and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_enabled, test))]
extern crate std;
mod api;
pub use api::*;
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
mod platform;
#[cfg(any(all(hal_enabled, not(target_os = "nuttx")), all(test, unix)))]
mod shared;
#[cfg(any(hal_enabled, all(test, unix)))]
mod worker;

pub use platform::acquisition::{open, open_for_quiescence, open_with_storage};

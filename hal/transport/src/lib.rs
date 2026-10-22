//! Packet output through shared Rust sockets.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};
#[cfg(any(hal_enabled, hal_mock, test))]
mod platform;
#[cfg(any(hal_enabled, test))]
mod udp;
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::*;

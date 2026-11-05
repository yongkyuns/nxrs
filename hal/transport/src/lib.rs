//! Packet-output contracts and explicit Rust socket operations.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(feature = "udp", test))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink};
mod platform;
#[cfg(feature = "udp")]
pub mod udp;

#[cfg(hal_mock)]
pub use platform::MockTransport;

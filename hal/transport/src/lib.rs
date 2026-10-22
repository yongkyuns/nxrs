//! Packet output through shared Rust sockets.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};
#[cfg(any(hal_enabled, test))]
mod udp;
#[cfg(hal_enabled)]
pub use udp::{transport, UdpSender, UdpTransport};
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(hal_mock)]
pub use mock::MockTransport;
#[cfg(hal_mock)]
pub fn open() -> impl Transport {
    MockTransport::default()
}
#[cfg(hal_enabled)]
mod platform;
#[cfg(hal_enabled)]
pub use platform::open;

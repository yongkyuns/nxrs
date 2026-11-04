//! Packet output through shared Rust sockets.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};
mod platform;
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub mod udp;

// Explicit shared functionality is independent of hardware admission.
#[cfg(any(hal_enabled, test, target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use udp::{UdpSender, UdpTransport};

// Legacy factory signatures and admission behavior remain unchanged.
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::open;
#[cfg(hal_mock)]
pub use platform::MockTransport;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use platform::transport;

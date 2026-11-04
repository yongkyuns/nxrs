//! Packet output through shared Rust sockets.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_mock, test))]
extern crate alloc;
#[cfg(any(
    hal_enabled,
    test,
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};
#[cfg(any(
    hal_enabled,
    hal_mock,
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "nuttx"
))]
mod platform;
#[cfg(any(
    hal_enabled,
    test,
    any(
        target_os = "linux",
        target_os = "macos",
        target_os = "windows",
        target_os = "nuttx"
    )
))]
#[allow(dead_code)]
pub mod udp;
#[cfg(any(hal_enabled, hal_mock))]
pub use platform::open;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx"))]
pub use platform::transport;
#[cfg(hal_mock)]
pub use platform::MockTransport;

// Explicit shared Rust functionality, independent of HAL admission.
#[cfg(any(hal_enabled, test, any(target_os = "linux", target_os = "macos", target_os = "windows", target_os = "nuttx")))]
pub use udp::{UdpSender, UdpTransport};

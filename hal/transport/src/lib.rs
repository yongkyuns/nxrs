//! Packet-output contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(
    nxrs_hal_platform = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate alloc;
#[cfg(any(
    nxrs_hal_platform = "std",
    nxrs_hal_platform = "nuttx",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};

#[cfg(any(
    nxrs_hal_platform = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(nxrs_hal_platform = "mock")]
pub use mock::MockTransport;

#[cfg(any(
    nxrs_hal_platform = "std",
    nxrs_hal_platform = "nuttx",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[path = "platform/std.rs"]
mod udp;
#[cfg(any(nxrs_hal_platform = "std", nxrs_hal_platform = "nuttx"))]
pub use udp::{transport, UdpSender, UdpTransport};

#[cfg(nxrs_hal_platform = "std")]
pub use udp::transport as open;

#[cfg(nxrs_hal_platform = "nuttx")]
pub fn open(address: [u8; 4], port: u16) -> Result<impl Transport, DeviceError> {
    UdpSender::connect(address, port)
}

#[cfg(nxrs_hal_platform = "mock")]
pub fn open() -> impl Transport {
    MockTransport::default()
}

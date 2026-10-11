//! Packet-output contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(
    nxrs_provider = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate alloc;
#[cfg(any(
    nxrs_provider = "native",
    nxrs_provider = "nuttx",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
extern crate std;

mod api;
pub use api::{DeviceError, PacketSink, Transport};

#[cfg(any(
    nxrs_provider = "mock",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[cfg_attr(test, allow(dead_code))]
#[path = "providers/mock.rs"]
mod mock;
#[cfg(nxrs_provider = "mock")]
pub use mock::MockTransport;

#[cfg(any(
    nxrs_provider = "native",
    nxrs_provider = "nuttx",
    all(
        test,
        any(target_os = "linux", target_os = "macos", target_os = "windows")
    )
))]
#[path = "providers/udp.rs"]
mod udp;
#[cfg(nxrs_provider = "native")]
pub use udp::{transport, UdpTransport};

#[cfg(nxrs_provider = "nuttx")]
pub use udp::UdpSender;

#[cfg(nxrs_provider = "native")]
pub use udp::transport as open;

#[cfg(nxrs_provider = "nuttx")]
pub fn open(address: [u8; 4], port: u16) -> Result<impl Transport, DeviceError> {
    UdpSender::connect(address, port)
}

#[cfg(nxrs_provider = "mock")]
pub fn open() -> impl Transport {
    MockTransport::default()
}

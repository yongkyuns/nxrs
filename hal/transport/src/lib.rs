//! Transport capability facade. Concrete provider selection stays inside this HAL.
#![no_std]
#![forbid(unsafe_code)]

pub use transport_api::{DeviceError, PacketSink, Transport};

#[cfg(any(
    all(feature = "mock", feature = "native"),
    all(feature = "mock", feature = "nuttx"),
    all(feature = "native", feature = "nuttx"),
))]
compile_error!("select at most one nxrs-transport provider feature");

#[cfg(feature = "native")]
pub use transport_native::transport as open;

#[cfg(feature = "nuttx")]
pub fn open(address: [u8; 4], port: u16) -> Result<impl Transport, DeviceError> {
    transport_nuttx::UdpSender::connect(address, port)
}

#[cfg(feature = "mock")]
pub fn open() -> impl Transport {
    transport_mock::MockTransport::default()
}

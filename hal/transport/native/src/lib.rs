//! Nonblocking UDP submission using std::net, without camera/storage providers.
#![forbid(unsafe_code)]
#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
compile_error!("native transport supports Linux, macOS and Windows only");

mod udp;
pub use udp::UdpTransport;
use std::io;
use std::net::{Ipv6Addr, SocketAddr};
use transport_api::{DeviceError, Transport};

pub fn transport(peer: &str) -> io::Result<impl Transport> {
    let peer: SocketAddr = peer.parse()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    let local = if peer.is_ipv4() {
        SocketAddr::from(([0, 0, 0, 0], 0))
    } else {
        SocketAddr::from((Ipv6Addr::UNSPECIFIED, 0))
    };
    UdpTransport::bind(local, peer)
}

fn device_error(error: io::Error) -> DeviceError {
    // Preserve the existing mapping; send handles transient socket errors first.
    match error.kind() {
        io::ErrorKind::TimedOut => DeviceError::Timeout,
        io::ErrorKind::Unsupported => DeviceError::Unsupported,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

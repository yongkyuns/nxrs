//! NuttX UDP packet submission through the qualified Rust standard library.
#![forbid(unsafe_code)]
#[cfg(not(target_os = "nuttx"))]
compile_error!("NuttX std transport requires target_os=nuttx");

use std::io;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};

use transport_api::{DeviceError, Transport};

/// Nonblocking IPv4 UDP submission using `std::net` directly on NuttX.
///
/// This provider exists only to adapt an optional packet-producing service to
/// the OS socket. It does not wrap the NuttX socket API or own a transport
/// runtime. The underlying `std::net::UdpSocket` path is qualified separately
/// against the pinned NuttX socket ABI before this provider is admitted.
pub struct UdpSender {
    socket: UdpSocket,
}

impl UdpSender {
    pub fn connect(address: [u8; 4], port: u16) -> Result<Self, DeviceError> {
        let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
        let peer = SocketAddrV4::new(Ipv4Addr::from(address), port);
        let socket = UdpSocket::bind(local).map_err(device_error)?;
        socket.connect(peer).map_err(device_error)?;
        socket.set_nonblocking(true).map_err(device_error)?;
        Ok(Self { socket })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }
}

impl Transport for UdpSender {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.len() > 65_507 {
            return Err(DeviceError::InvalidData);
        }
        match self.socket.send(packet) {
            Ok(len) if len == packet.len() => Ok(()),
            Ok(_) => Err(DeviceError::Io),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                Err(DeviceError::Busy)
            }
            Err(error) => Err(device_error(error)),
        }
    }
}

fn device_error(error: io::Error) -> DeviceError {
    match error.kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted => DeviceError::Busy,
        io::ErrorKind::TimedOut => DeviceError::Timeout,
        io::ErrorKind::Unsupported => DeviceError::Unsupported,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

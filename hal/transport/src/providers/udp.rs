use super::device_error;
use crate::{DeviceError, Transport};
use std::io;
use std::net::{SocketAddr, UdpSocket};

/// Nonblocking local datagram submission, not remote delivery acknowledgement.
/// Numeric SocketAddr inputs keep name resolution out of send().
pub struct UdpTransport {
    socket: UdpSocket,
}

impl UdpTransport {
    pub fn bind(local: SocketAddr, peer: SocketAddr) -> io::Result<Self> {
        let socket = UdpSocket::bind(local)?;
        socket.connect(peer)?;
        socket.set_nonblocking(true)?;
        Ok(Self { socket })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }
}

impl Transport for UdpTransport {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.is_empty() || packet.len() > 65_507 {
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

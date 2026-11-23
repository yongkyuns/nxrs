//! Shared std UDP I/O; target socket ABI details belong to the standard library.
#![forbid(unsafe_code)]

use super::{DeviceError, PacketSink};
use std::io;
use std::net::Ipv6Addr;
use std::net::{SocketAddr, UdpSocket};

const MAX_PAYLOAD: usize = 65_507;

/// Nonblocking local datagram submission, not remote delivery acknowledgement.
/// Numeric SocketAddr inputs keep name resolution out of send().
pub struct UdpTransport {
    socket: UdpSocket,
}

impl UdpTransport {
    /// Connect a numeric peer from an ephemeral local port of the same IP family.
    /// This explicit std operation needs no configured hardware provider.
    pub fn connect(peer: SocketAddr) -> io::Result<Self> {
        let local = if peer.is_ipv4() {
            SocketAddr::from(([0, 0, 0, 0], 0))
        } else {
            SocketAddr::from((Ipv6Addr::UNSPECIFIED, 0))
        };
        Self::bind(local, peer)
    }

    pub fn bind(local: SocketAddr, peer: SocketAddr) -> io::Result<Self> {
        let socket = UdpSocket::bind(local)?;
        socket.connect(peer)?;
        socket.set_nonblocking(true)?;
        Ok(Self { socket })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }

    fn submit(&self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.len() > MAX_PAYLOAD {
            return Err(DeviceError::InvalidData);
        }
        submission_result(self.socket.send(packet), packet.len())
    }
}

impl PacketSink for UdpTransport {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.is_empty() {
            return Err(DeviceError::InvalidData);
        }
        self.submit(packet)
    }
}

fn submission_result(result: io::Result<usize>, expected: usize) -> Result<(), DeviceError> {
    match result {
        Ok(len) if len == expected => Ok(()),
        Ok(_) => Err(DeviceError::Io),
        Err(error) => Err(device_error(error)),
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn submission_status_preserves_transient_and_terminal_errors() {
        assert_eq!(submission_result(Ok(0), 0), Ok(()));
        assert_eq!(submission_result(Ok(3), 4), Err(DeviceError::Io));
        for (kind, expected) in [
            (io::ErrorKind::WouldBlock, DeviceError::Busy),
            (io::ErrorKind::Interrupted, DeviceError::Busy),
            (io::ErrorKind::TimedOut, DeviceError::Timeout),
            (io::ErrorKind::Unsupported, DeviceError::Unsupported),
            (io::ErrorKind::InvalidInput, DeviceError::InvalidData),
            (io::ErrorKind::InvalidData, DeviceError::InvalidData),
            (io::ErrorKind::PermissionDenied, DeviceError::Io),
        ] {
            assert_eq!(submission_result(Err(kind.into()), 4), Err(expected));
        }
    }

    #[test]
    fn fixture_rejects_invalid_packets_and_preserves_source_port() {
        let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut sender = UdpTransport::connect(receiver.local_addr().unwrap()).unwrap();
        assert_eq!(sender.send(&[]), Err(DeviceError::InvalidData));
        assert_eq!(
            sender.send(&std::vec![0; MAX_PAYLOAD + 1]),
            Err(DeviceError::InvalidData)
        );
        sender.send(b"camera-fixture").unwrap();
        let mut bytes = [0; 16];
        let (len, peer) = receiver.recv_from(&mut bytes).unwrap();
        assert_eq!(&bytes[..len], b"camera-fixture");
        assert_eq!(peer.port(), sender.local_addr().unwrap().port());
        assert!(peer.ip().is_loopback());
    }
}

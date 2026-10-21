//! Shared std UDP I/O; target socket ABI details belong to the standard library.
#![forbid(unsafe_code)]

use crate::{DeviceError, Transport};
use std::io;
use std::net::Ipv6Addr;
use std::net::{Ipv4Addr, SocketAddrV4};
use std::net::{SocketAddr, UdpSocket};

const MAX_PAYLOAD: usize = 65_507;

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

    fn submit(&self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.len() > MAX_PAYLOAD {
            return Err(DeviceError::InvalidData);
        }
        submission_result(self.socket.send(packet), packet.len())
    }
}

impl Transport for UdpTransport {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        if packet.is_empty() {
            return Err(DeviceError::InvalidData);
        }
        self.submit(packet)
    }
}

/// Compatibility IPv4 constructor for the qualified NuttX fixtures.
/// Its zero-length datagram contract differs from the native PacketSink facade.
pub struct UdpSender {
    socket: UdpTransport,
}

impl UdpSender {
    pub fn connect(address: [u8; 4], port: u16) -> Result<Self, DeviceError> {
        let local = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, 0);
        let peer = SocketAddrV4::new(Ipv4Addr::from(address), port);
        UdpTransport::bind(local.into(), peer.into())
            .map(|socket| Self { socket })
            .map_err(device_error)
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }
}

impl Transport for UdpSender {
    fn send(&mut self, packet: &[u8]) -> Result<(), DeviceError> {
        self.socket.submit(packet)
    }
}

pub fn transport(peer: &str) -> io::Result<impl Transport> {
    let peer: SocketAddr = peer
        .parse()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    let local = if peer.is_ipv4() {
        SocketAddr::from(([0, 0, 0, 0], 0))
    } else {
        SocketAddr::from((Ipv6Addr::UNSPECIFIED, 0))
    };
    UdpTransport::bind(local, peer)
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
        assert!(transport("not-a-numeric-address:9000").is_err());
    }

    #[test]
    fn ipv4_facade_keeps_its_empty_datagram_and_ownership_contract() {
        let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut sender =
            UdpSender::connect([127, 0, 0, 1], receiver.local_addr().unwrap().port()).unwrap();
        let mut bytes = [0; 16];
        for payload in [&[][..], &b"shared-std"[..]] {
            sender.send(payload).unwrap();
            let (len, peer) = receiver.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..len], payload);
            assert_eq!(peer.port(), sender.local_addr().unwrap().port());
            assert!(peer.ip().is_loopback());
        }
        assert_eq!(
            sender.send(&std::vec![0; MAX_PAYLOAD + 1]),
            Err(DeviceError::InvalidData)
        );
        receiver.set_nonblocking(true).unwrap();
        assert_eq!(
            receiver.recv(&mut bytes).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        // This exercises shared Rust socket code on a host, not the NuttX ABI.
    }
}

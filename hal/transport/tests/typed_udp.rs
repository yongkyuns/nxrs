//! Numeric-address construction is the same operation under unset/mock/native admission.
#![cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]

use std::net::UdpSocket;
use std::time::Duration;
use transport::udp::UdpTransport;
use transport::{DeviceError, PacketSink};

#[test]
fn typed_connect_selects_an_ephemeral_local_socket_and_keeps_packet_semantics() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut sender = UdpTransport::connect(receiver.local_addr().unwrap()).unwrap();
    sender.send(b"typed-peer").unwrap();
    let mut bytes = [0; 32];
    let (len, peer) = receiver.recv_from(&mut bytes).unwrap();
    assert_eq!(&bytes[..len], b"typed-peer");
    assert!(peer.ip().is_loopback());
    assert_eq!(peer.port(), sender.local_addr().unwrap().port());
    assert_ne!(peer.port(), 0);
    assert_eq!(sender.send(&[]), Err(DeviceError::InvalidData));
    assert_eq!(sender.send(&vec![0; 65_508]), Err(DeviceError::InvalidData));
    receiver.set_nonblocking(true).unwrap();
    assert_eq!(
        receiver.recv(&mut bytes).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#![cfg(nxrs_provider = "native")]

use std::net::UdpSocket;
use std::time::Duration;

use transport::{transport, DeviceError, Transport, UdpTransport};

#[test]
fn independent_sockets_preserve_packet_bytes_and_peer_ownership() {
    let receiver_a = UdpSocket::bind("127.0.0.1:0").unwrap();
    let receiver_b = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver_a
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    receiver_b
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut a = UdpTransport::bind(
        "127.0.0.1:0".parse().unwrap(),
        receiver_a.local_addr().unwrap(),
    )
    .unwrap();
    let mut b = UdpTransport::bind(
        "127.0.0.1:0".parse().unwrap(),
        receiver_b.local_addr().unwrap(),
    )
    .unwrap();
    assert_ne!(a.local_addr().unwrap(), b.local_addr().unwrap());
    let mut bytes = [0; 16];
    a.send(&[0, 127, 128, 255]).unwrap();
    let (n, peer) = receiver_a.recv_from(&mut bytes).unwrap();
    assert_eq!(&bytes[..n], &[0, 127, 128, 255]);
    assert_eq!(peer, a.local_addr().unwrap());
    drop(a);
    b.send(b"still-owned").unwrap();
    let (n, peer) = receiver_b.recv_from(&mut bytes).unwrap();
    assert_eq!(&bytes[..n], b"still-owned");
    assert_eq!(peer, b.local_addr().unwrap());
}

#[test]
fn rejected_packets_are_not_submitted_and_factory_rejects_non_numeric_addresses() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver.set_nonblocking(true).unwrap();
    let mut sender = transport(&receiver.local_addr().unwrap().to_string()).unwrap();
    assert_eq!(sender.send(&[]), Err(DeviceError::InvalidData));
    let oversized = vec![0; 65_508]; // Keep the large test payload off the stack.
    assert_eq!(sender.send(&oversized), Err(DeviceError::InvalidData));
    assert_eq!(
        receiver.recv(&mut [0; 1]).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    assert!(transport("not-a-numeric-address:9000").is_err());
}

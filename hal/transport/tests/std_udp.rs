//! Real loopback UDP is explicit shared functionality, not a radio backend.
use std::net::UdpSocket;
use std::time::{Duration, Instant};

use transport::{DeviceError, PacketSink};

fn send_bounded(output: &mut impl PacketSink, bytes: &[u8]) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match output.send(bytes) {
            Ok(()) => return,
            Err(DeviceError::Busy) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(1));
            }
            result => panic!("datagram submission did not complete: {result:?}"),
        }
    }
}

#[test]
fn explicit_udp_constructor_delivers_exact_payload_without_hardware_admission() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut output = transport::udp::UdpTransport::connect(receiver.local_addr().unwrap()).unwrap();
    assert_eq!(output.send(&[]), Err(DeviceError::InvalidData));
    let payload = b"shared-std-without-a-radio";
    send_bounded(&mut output, payload);
    let mut bytes = [0; 64];
    let (len, peer) = receiver.recv_from(&mut bytes).unwrap();
    assert!(peer.ip().is_loopback());
    assert_eq!(&bytes[..len], payload);
}

#[test]
fn explicit_ipv4_sender_retains_empty_datagram_contract() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut output =
        transport::udp::UdpSender::connect([127, 0, 0, 1], receiver.local_addr().unwrap().port())
            .unwrap();
    send_bounded(&mut output, &[]);
    assert_eq!(receiver.recv(&mut [0; 1]).unwrap(), 0);
}

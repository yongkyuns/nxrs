//! Untrusted LAN locator hints. Only the commissioned TLS pin authenticates a peer.
use std::net::{SocketAddr, SocketAddrV4};

pub const SIZE: usize = 36;
pub fn query(pin: &[u8; 32]) -> [u8; SIZE] {
    let mut packet = [0; SIZE];
    packet[..4].copy_from_slice(b"NXDQ");
    packet[4..].copy_from_slice(pin);
    packet
}
pub fn reply(packet: &[u8], pin: &[u8; 32]) -> Option<[u8; SIZE]> {
    if packet != query(pin) {
        return None;
    }
    let mut result = query(pin);
    result[..4].copy_from_slice(b"NXDR");
    Some(result)
}
pub fn local_peer(peer: SocketAddr) -> Option<SocketAddrV4> {
    match peer {
        SocketAddr::V4(peer)
            if peer.port() != 0 && (peer.ip().is_private() || peer.ip().is_loopback()) =>
        {
            Some(peer)
        }
        _ => None,
    }
}
pub fn candidate(
    packet: &[u8],
    peer: SocketAddr,
    pin: &[u8; 32],
    port: u16,
) -> Option<SocketAddrV4> {
    let peer = local_peer(peer)?;
    let expected = reply(&query(pin), pin)?;
    (peer.port() == port && packet == expected).then_some(peer)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_public_pin_packets_only_provide_local_endpoint_hints() {
        let pin = [3; 32];
        let request = query(&pin);
        let response = reply(&request, &pin).unwrap();
        let peer = "192.168.1.9:8444".parse().unwrap();
        assert_eq!(candidate(&response, peer, &pin, 8444), local_peer(peer));
        assert!(reply(&request, &[4; 32]).is_none());
        for packet in [&response[..35], &[response.as_slice(), &[0]].concat()] {
            assert!(candidate(packet, peer, &pin, 8444).is_none());
        }
        for peer in [
            "8.8.8.8:8444",
            "224.0.0.1:8444",
            "[::1]:8444",
            "192.168.1.9:1",
        ] {
            assert!(candidate(&response, peer.parse().unwrap(), &pin, 8444).is_none());
        }
    }
}

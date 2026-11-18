//! Bounded SNTPv4 queries on a socket selected by the deployment.
use std::{
    io,
    net::{Ipv6Addr, SocketAddrV6},
    time::{Duration, Instant, SystemTime},
};
use tokio::{net::UdpSocket, time::timeout};

const NTP_PORT: u16 = 123;
const NTP_HEADER: usize = 48;
const UNIX_EPOCH_NTP: i128 = 2_208_988_800;
const MATTER_EPOCH_NTP: i128 = 3_155_673_600;
const MICROS_PER_SECOND: i128 = 1_000_000;
const NTP_ERA: i128 = 1 << 32;
const QUERY_DEADLINE: Duration = Duration::from_secs(3);

pub fn server_address(address: Ipv6Addr, interface: u32) -> SocketAddrV6 {
    let scope = if address.is_unicast_link_local() {
        interface
    } else {
        0
    };
    SocketAddrV6::new(address, NTP_PORT, 0, scope)
}

/// Return Matter epoch microseconds from the configured NTP endpoint.
/// The connected UDP socket and echoed request timestamp bind the response
/// to this query. This transport is unauthenticated SNTP.
pub async fn sample(socket: UdpSocket, peer: SocketAddrV6, nonce: [u8; 8]) -> io::Result<u64> {
    socket.connect(peer).await?;
    let mut request = [0; NTP_HEADER];
    request[0] = 0x23; // Version 4, client.
    request[40..].copy_from_slice(&nonce);

    let sent = Instant::now();
    let mut reply = [0; 512];
    let size = timeout(QUERY_DEADLINE, async {
        socket.send(&request).await?;
        socket.recv(&mut reply).await
    })
    .await
    .map_err(|_| io::ErrorKind::TimedOut)??;
    let era_hint = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_err(|_| io::ErrorKind::InvalidData)?
        .as_secs();
    sample_time(&reply[..size], nonce, era_hint, sent.elapsed())
        .ok_or(io::ErrorKind::InvalidData.into())
}

fn timestamp(bytes: &[u8], unix_hint: u64) -> Option<i128> {
    let seconds = i128::from(u32::from_be_bytes(bytes[..4].try_into().ok()?));
    let fraction = i128::from(u32::from_be_bytes(bytes[4..].try_into().ok()?));
    if seconds == 0 && fraction == 0 {
        return None;
    }
    // The nearest era also handles a server timestamp that wraps in 2036.
    let hint = i128::from(unix_hint) + UNIX_EPOCH_NTP;
    let era = (hint - seconds + NTP_ERA / 2).div_euclid(NTP_ERA);
    let whole = (seconds + era * NTP_ERA - MATTER_EPOCH_NTP) * MICROS_PER_SECOND;
    Some(whole + (fraction * MICROS_PER_SECOND) / NTP_ERA)
}

fn sample_time(reply: &[u8], nonce: [u8; 8], unix_hint: u64, elapsed: Duration) -> Option<u64> {
    let header = reply.get(..NTP_HEADER)?;
    if header[0] >> 6 == 3
        || header[0] & 0x3f != 0x24
        || !(1..=15).contains(&header[1])
        || header[24..32] != nonce
    {
        return None;
    }
    let received_at = timestamp(&header[32..40], unix_hint)?;
    let sent_at = timestamp(&header[40..48], unix_hint)?;
    let server_time = sent_at.checked_sub(received_at)?;
    let elapsed = i128::try_from(elapsed.as_micros()).ok()?;
    if !(0..=elapsed).contains(&server_time) {
        return None;
    }
    // Estimate time at receipt with monotonic RTT, without setting the host clock.
    u64::try_from(sent_at + (elapsed - server_time) / 2).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn response(nonce: [u8; 8], seconds: u32) -> [u8; NTP_HEADER] {
        let mut reply = [0; NTP_HEADER];
        reply[0] = 0x24;
        reply[1] = 2;
        reply[24..32].copy_from_slice(&nonce);
        reply[32..36].copy_from_slice(&seconds.to_be_bytes());
        reply[40..44].copy_from_slice(&seconds.to_be_bytes());
        reply
    }

    #[test]
    fn validates_sender_response_and_unfolds_first_era_rollover() {
        let token = *b"12345678";
        let hint = 2_085_978_497;
        let reply = response(token, 1);
        let delay = Duration::from_millis(100);
        assert_eq!(
            sample_time(&reply, token, hint, delay),
            Some(1_139_293_697_050_000)
        );
        for (offset, value) in [(0, 0xe4), (0, 0x23), (1, 0), (1, 16), (24, 0), (32, 1)] {
            let mut invalid = reply;
            invalid[offset] = value;
            assert_eq!(sample_time(&invalid, token, hint, delay), None);
        }
        assert_eq!(sample_time(&reply[..47], token, hint, delay), None);
        assert_eq!(
            server_address("fe80::1".parse().unwrap(), 12).scope_id(),
            12
        );
        assert_eq!(server_address(Ipv6Addr::LOCALHOST, 12).scope_id(), 0);
    }

    #[tokio::test]
    async fn receives_a_fresh_sample_over_udp() {
        use std::net::SocketAddr;

        let server = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let SocketAddr::V6(peer) = server.local_addr().unwrap() else {
            panic!("expected IPv6 listener");
        };
        let client = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let nonce = *b"87654321";
        let utc = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let seconds = (i128::from(utc) + UNIX_EPOCH_NTP).rem_euclid(NTP_ERA) as u32;
        let exchange = async {
            let mut request = [0; NTP_HEADER];
            let (size, client) = server.recv_from(&mut request).await.unwrap();
            assert_eq!(size, NTP_HEADER);
            assert_eq!(request[0], 0x23);
            assert_eq!(request[40..], nonce);
            server
                .send_to(&response(nonce, seconds), client)
                .await
                .unwrap();
        };
        let (accepted, ()) = tokio::join!(sample(client, peer, nonce), exchange);
        let expected = (i128::from(utc) + UNIX_EPOCH_NTP - MATTER_EPOCH_NTP) * MICROS_PER_SECOND;
        assert!((expected as u64..expected as u64 + 1_000_000).contains(&accepted.unwrap()));
    }

    #[tokio::test(start_paused = true)]
    async fn stops_waiting_when_the_server_is_silent() {
        let server = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let std::net::SocketAddr::V6(peer) = server.local_addr().unwrap() else {
            panic!("expected IPv6 listener");
        };
        let client = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let error = sample(client, peer, *b"12345678").await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }
}

use super::Lan;
use nix::sys::socket::{
    ControlMessageOwned, MsgFlags, SockaddrStorage, cmsg_space, recvmsg, setsockopt, sockopt,
};
use std::{
    io::{self, IoSliceMut},
    net::{IpAddr, SocketAddr, SocketAddrV6, UdpSocket},
    os::fd::AsRawFd,
};

pub(super) fn tcp_addresses(lan: Lan, port: u16) -> io::Result<Vec<SocketAddr>> {
    // IPV6_BOUND_IF does not exclude local traffic to ::1 on wildcard listeners.
    let mut addresses = vec![(lan.ipv4(), port).into()];
    for interface in if_addrs::get_if_addrs()? {
        if interface.index != Some(lan.index()) {
            continue;
        }
        if let IpAddr::V6(ip) = interface.ip() {
            let socket = if ip.is_unicast_link_local() {
                // Darwin can embed the link-local zone in the address returned by getifaddrs.
                let mut octets = ip.octets();
                octets[2..4].fill(0);
                SocketAddrV6::new(octets.into(), port, 0, lan.index())
            } else {
                SocketAddrV6::new(ip, port, 0, 0)
            };
            addresses.push(socket.into());
        }
    }
    addresses.sort_unstable();
    addresses.dedup();
    if addresses.len() == 1 {
        return Err(io::Error::new(
            io::ErrorKind::AddrNotAvailable,
            "Matter LAN IPv6 address",
        ));
    }
    Ok(addresses)
}

pub(super) fn enable_packet_info(socket: &UdpSocket, ipv6: bool) -> io::Result<()> {
    if ipv6 {
        setsockopt(socket, sockopt::Ipv6RecvPacketInfo, &true)?;
    } else {
        setsockopt(socket, sockopt::Ipv4PacketInfo, &true)?;
    }
    Ok(())
}

const PACKET_INFO_BYTES: usize =
    cmsg_space::<libc::in_pktinfo>() + cmsg_space::<libc::in6_pktinfo>();

#[repr(align(8))]
struct PacketInfo([u8; PACKET_INFO_BYTES]);

pub(super) fn recv_matter_udp(
    lan: Lan,
    socket: &UdpSocket,
    buffer: &mut [u8],
) -> io::Result<Option<(usize, SocketAddr)>> {
    let mut control = PacketInfo([0; PACKET_INFO_BYTES]);
    let mut data = [IoSliceMut::new(buffer)];
    let msg = recvmsg::<SockaddrStorage>(
        socket.as_raw_fd(),
        &mut data,
        Some(&mut control.0),
        MsgFlags::empty(),
    )?;
    if msg
        .flags
        .intersects(MsgFlags::MSG_TRUNC | MsgFlags::MSG_CTRUNC)
    {
        return Ok(None);
    }
    let ingress = msg.cmsgs()?.find_map(|info| match info {
        ControlMessageOwned::Ipv4PacketInfo(info) => Some(info.ipi_ifindex),
        ControlMessageOwned::Ipv6PacketInfo(info) => Some(info.ipi6_ifindex),
        _ => None,
    });
    if ingress != Some(lan.index()) {
        return Ok(None);
    }
    let peer = msg.address.and_then(|addr| {
        addr.as_sockaddr_in()
            .copied()
            .map(SocketAddr::from)
            .or_else(|| addr.as_sockaddr_in6().copied().map(SocketAddr::from))
    });
    Ok(peer.map(|peer| (msg.bytes, peer)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        net::{Ipv4Addr, Ipv6Addr},
        num::NonZeroU32,
        time::Duration,
    };

    #[test]
    fn receive_accepts_only_the_selected_interface_in_both_address_families() {
        let loopback = if_addrs::get_if_addrs()
            .unwrap()
            .into_iter()
            .find(|interface| interface.ip() == IpAddr::V4(Ipv4Addr::LOCALHOST))
            .and_then(|interface| interface.index)
            .and_then(NonZeroU32::new)
            .unwrap();
        let allowed = Lan {
            index: loopback,
            ipv4: Ipv4Addr::LOCALHOST,
        };
        let excluded = Lan {
            index: NonZeroU32::MAX,
            ..allowed
        };
        let payload = b"interface-scoped UDP";
        for host in [
            IpAddr::from(Ipv4Addr::LOCALHOST),
            IpAddr::from(Ipv6Addr::LOCALHOST),
        ] {
            let receiver = UdpSocket::bind((host, 0)).unwrap();
            receiver
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            enable_packet_info(&receiver, host.is_ipv6()).unwrap();
            let sender = UdpSocket::bind((host, 0)).unwrap();
            let local = receiver.local_addr().unwrap();
            let mut buffer = [0; 32];

            sender.send_to(payload, local).unwrap();
            assert_eq!(
                recv_matter_udp(excluded, &receiver, &mut buffer).unwrap(),
                None
            );
            sender.send_to(payload, local).unwrap();
            assert_eq!(
                recv_matter_udp(allowed, &receiver, &mut buffer).unwrap(),
                Some((payload.len(), sender.local_addr().unwrap()))
            );
            assert_eq!(&buffer[..payload.len()], payload);
        }
    }
}

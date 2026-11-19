use super::Lan;
use std::{
    io,
    net::{Ipv4Addr, Ipv6Addr, SocketAddr, UdpSocket},
};

pub(super) fn tcp_addresses(_lan: Lan, port: u16) -> io::Result<[SocketAddr; 2]> {
    Ok([
        (Ipv4Addr::UNSPECIFIED, port).into(),
        (Ipv6Addr::UNSPECIFIED, port).into(),
    ])
}

pub(super) fn enable_packet_info(_socket: &UdpSocket, _ipv6: bool) -> io::Result<()> {
    Ok(())
}

pub(super) fn recv_matter_udp(
    _lan: Lan,
    socket: &UdpSocket,
    buffer: &mut [u8],
) -> io::Result<Option<(usize, SocketAddr)>> {
    // Linux's bound-device socket filters received datagrams in the kernel.
    socket.recv_from(buffer).map(Some)
}

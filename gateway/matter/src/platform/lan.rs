//! Resolve the configured LAN once and scope all Matter sockets to it.

use socket2::{Domain, Protocol, SockRef, Socket, Type};
use std::{
    io,
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream, UdpSocket},
    num::NonZeroU32,
};

#[derive(Clone, Copy)]
pub struct Lan {
    index: NonZeroU32,
    ipv4: Ipv4Addr,
}

impl Lan {
    pub fn select(ipv4: Ipv4Addr, name: Option<&str>) -> io::Result<Self> {
        let interfaces = if_addrs::get_if_addrs()?;
        let mut candidates = interfaces.iter().filter(|interface| {
            !interface.is_loopback()
                && !interface.is_p2p
                && interface.ip() == IpAddr::V4(ipv4)
                && name.is_none_or(|name| name == interface.name)
        });
        let Some(interface) = candidates.next() else {
            return Err(io::Error::new(
                io::ErrorKind::AddrNotAvailable,
                "Matter LAN IPv4 address",
            ));
        };
        let index = interface
            .index
            .and_then(NonZeroU32::new)
            .filter(|index| {
                candidates.next().is_none()
                    && interfaces.iter().any(|other| {
                        other.index == Some(index.get())
                            && other.ip().is_ipv6()
                            && !other.is_loopback()
                    })
            })
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::AddrNotAvailable,
                    "expected one Matter LAN with IPv6",
                )
            })?;
        Ok(Self { index, ipv4 })
    }

    pub fn index(self) -> u32 {
        self.index.get()
    }

    pub fn ipv4(self) -> Ipv4Addr {
        self.ipv4
    }

    /// Read time servers supplied by the host's DHCP client for this link.
    pub async fn dhcp_time_servers(self) -> Vec<IpAddr> {
        super::dhcp::time_servers(self.index(), self.ipv4()).await
    }

    fn socket(self, addr: SocketAddr, kind: Type, protocol: Protocol) -> io::Result<Socket> {
        let socket = Socket::new(Domain::for_address(addr), kind, Some(protocol))?;
        if addr.is_ipv6() {
            // Darwin's interface options apply to one address family per socket.
            socket.set_only_v6(true)?;
            socket.bind_device_by_index_v6(Some(self.index))?;
        } else {
            socket.bind_device_by_index_v4(Some(self.index))?;
        }
        Ok(socket)
    }

    pub fn bind_udp(self, addr: SocketAddr) -> io::Result<UdpSocket> {
        let socket = self.socket(addr, Type::DGRAM, Protocol::UDP)?;
        if addr.is_ipv6() {
            socket.set_multicast_if_v6(self.index())?;
        } else {
            socket.set_multicast_if_v4(&self.ipv4)?;
        }
        socket.bind(&addr.into())?;
        Ok(socket.into())
    }

    pub fn bind_mdns_query(self, addr: SocketAddr) -> io::Result<UdpSocket> {
        let socket = self.bind_udp(addr)?;
        if addr.is_ipv6() {
            SockRef::from(&socket).set_multicast_hops_v6(255)?;
        } else {
            socket.set_multicast_ttl_v4(255)?;
        }
        socket.set_nonblocking(true)?;
        Ok(socket)
    }

    pub fn listen_tcp(self, addr: SocketAddr) -> io::Result<TcpListener> {
        let socket = self.socket(addr, Type::STREAM, Protocol::TCP)?;
        socket.set_reuse_address(true)?;
        socket.bind(&addr.into())?;
        socket.listen(16)?;
        Ok(socket.into())
    }

    pub async fn connect_tcp(self, addr: SocketAddr) -> io::Result<TcpStream> {
        let socket = self.socket(addr, Type::STREAM, Protocol::TCP)?;
        socket.set_nonblocking(true)?;
        tokio::net::TcpSocket::from_std_stream(socket.into())
            .connect(addr)
            .await?
            .into_std()
    }
}

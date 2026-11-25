//! Bounded DNS-SD for NTP on an interface-bound UDP socket.
use domain::{
    base::{
        iana::{Class, Opcode},
        Message, MessageBuilder, Name, Rtype, ToName,
    },
    rdata::{Aaaa, Ptr, Srv, A},
};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};
use tokio::{net::UdpSocket, time::timeout};

const SERVICE: &str = "_ntp._udp.local.";
const PACKET_LIMIT: usize = 1232;
const RECORD_LIMIT: usize = 32;
const QUESTION_DEADLINE: Duration = Duration::from_millis(750);
const DISCOVERY_DEADLINE: Duration = Duration::from_secs(2);

type DnsName = Name<Vec<u8>>;

enum Data {
    Instance(DnsName),
    Server(DnsName, u16),
    Address(IpAddr),
}

struct Record {
    owner: DnsName,
    kind: Rtype,
    data: Data,
}

fn unpack(packet: &[u8], id: u16) -> Option<Vec<Record>> {
    let message = Message::from_octets(packet).ok()?;
    let header = message.header();
    let flags = header.flags();
    if header.id() != id
        || !flags.qr
        || flags.tc
        || header.opcode() != Opcode::QUERY
        || header.rcode().to_int() != 0
    {
        return None;
    }
    let mut records = Vec::new();
    for record in [message.answer().ok()?, message.additional().ok()?]
        .into_iter()
        .flatten()
        .take(RECORD_LIMIT)
    {
        let record = record.ok()?;
        if record.ttl().as_secs() == 0 || (record.class().to_int() & 0x7fff) != Class::IN.to_int() {
            continue;
        }
        let data = if let Some(value) = record.to_record::<Ptr<_>>().ok()? {
            Data::Instance(value.data().ptrdname().to_name())
        } else if let Some(value) = record.to_record::<Srv<_>>().ok()? {
            Data::Server(value.data().target().to_name(), value.data().port())
        } else if let Some(value) = record.to_record::<A>().ok()? {
            Data::Address(value.data().addr().into())
        } else if let Some(value) = record.to_record::<Aaaa>().ok()? {
            Data::Address(value.data().addr().into())
        } else {
            continue;
        };
        records.push(Record {
            owner: record.owner().to_name(),
            kind: record.rtype(),
            data,
        });
    }
    Some(records)
}

struct Client {
    socket: UdpSocket,
    multicast: SocketAddr,
    id: u16,
}

impl Client {
    async fn lookup(&mut self, name: &DnsName, kind: Rtype) -> Option<Vec<Record>> {
        let id = self.id;
        self.id = self.id.wrapping_add(1);
        let mut query = MessageBuilder::from_target(Vec::<u8>::new())
            .ok()?
            .question();
        query.header_mut().set_id(id);
        query.push((name, kind, Class::IN)).ok()?;
        let request = query.finish();
        let mut reply = [0; PACKET_LIMIT];
        self.socket.send_to(&request, self.multicast).await.ok()?;
        timeout(QUESTION_DEADLINE, async {
            for _ in 0..RECORD_LIMIT {
                let (size, source) = self.socket.recv_from(&mut reply).await.ok()?;
                if source.port() != self.multicast.port() || size == reply.len() {
                    continue;
                }
                let Some(records) = unpack(&reply[..size], id) else {
                    continue;
                };
                if records
                    .iter()
                    .any(|rec| rec.kind == kind && rec.owner == *name)
                {
                    return Some(records);
                }
            }
            None
        })
        .await
        .ok()?
    }

    async fn discover(&mut self) -> Option<IpAddr> {
        let service = SERVICE.parse::<DnsName>().ok()?;
        let browse = self.lookup(&service, Rtype::PTR).await?;
        for name in browse
            .iter()
            .filter_map(|record| match &record.data {
                Data::Instance(name) if record.owner == service => Some(name),
                _ => None,
            })
            .take(4)
        {
            let resolved;
            let records = if browse
                .iter()
                .any(|rec| rec.owner == *name && rec.kind == Rtype::SRV)
            {
                &browse
            } else {
                let Some(reply) = self.lookup(name, Rtype::SRV).await else {
                    continue;
                };
                resolved = reply;
                &resolved
            };
            let Some(host) = records.iter().find_map(|record| match &record.data {
                Data::Server(host, super::NTP_PORT) if record.owner == *name => Some(host),
                _ => None,
            }) else {
                continue;
            };
            let ipv6 = self.multicast.is_ipv6();
            let find_address = |records: &[Record]| {
                records.iter().find_map(|record| match record.data {
                    Data::Address(addr)
                        if record.owner == *host
                            && addr.is_ipv6() == ipv6
                            && !addr.is_unspecified()
                            && !addr.is_loopback()
                            && !addr.is_multicast() =>
                    {
                        Some(addr)
                    }
                    _ => None,
                })
            };
            if let Some(addr) = find_address(records) {
                return Some(addr);
            }
            let kind = if ipv6 { Rtype::AAAA } else { Rtype::A };
            if let Some(addr) = self
                .lookup(host, kind)
                .await
                .as_deref()
                .and_then(find_address)
            {
                return Some(addr);
            }
        }
        None
    }
}

/// Browse for a unicast NTP server of the socket's address family.
///
/// An ephemeral source port requests legacy-unicast mDNS responses. This avoids
/// competing with the system responder for port 5353. The caller owns LAN binding.
pub async fn discover(socket: UdpSocket, multicast: SocketAddr, id: u16) -> Option<IpAddr> {
    timeout(
        DISCOVERY_DEADLINE,
        Client {
            socket,
            multicast,
            id,
        }
        .discover(),
    )
    .await
    .ok()?
}

#[cfg(test)]
mod tests {
    use super::*;
    use domain::base::{header::Flags, Ttl};
    use std::net::Ipv6Addr;

    #[tokio::test]
    async fn resolves_separate_records_and_ignores_unrelated_replies() {
        let server = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let client = UdpSocket::bind((Ipv6Addr::LOCALHOST, 0)).await.unwrap();
        let peer = server.local_addr().unwrap();
        let service = SERVICE.parse::<DnsName>().unwrap();
        let instance = "Reference._ntp._udp.local.".parse::<DnsName>().unwrap();
        let host = "clock.local.".parse::<DnsName>().unwrap();
        let other_name = "unrelated._ntp._udp.local.".parse::<DnsName>().unwrap();
        let address = "fe80::42".parse::<Ipv6Addr>().unwrap();
        let other_address = "fe80::bad".parse::<Ipv6Addr>().unwrap();
        let exchange = async {
            let mut buf = [0; PACKET_LIMIT];
            for (name, kind) in [
                (&service, Rtype::PTR),
                (&instance, Rtype::SRV),
                (&host, Rtype::AAAA),
            ] {
                let (size, client) = server.recv_from(&mut buf).await.unwrap();
                let request = Message::from_octets(&buf[..size]).unwrap();
                let question = request.question().next().unwrap().unwrap();
                assert_eq!(question.qname(), name);
                assert_eq!(question.qtype(), kind);
                let id = request.header().id();
                let build = |id, ttl, expected| {
                    let mut response = MessageBuilder::from_target(Vec::new()).unwrap();
                    let mut flags = Flags::new();
                    flags.qr = true;
                    response.header_mut().set_flags(flags);
                    response.header_mut().set_id(id);
                    let mut answer = response.answer();
                    match kind {
                        Rtype::PTR => answer
                            .push((
                                name,
                                Class::IN,
                                ttl,
                                Ptr::new(if expected { &instance } else { &other_name }),
                            ))
                            .unwrap(),
                        Rtype::SRV => answer
                            .push((
                                name,
                                Class::IN,
                                ttl,
                                Srv::new(0, 0, 123, if expected { &host } else { &other_name }),
                            ))
                            .unwrap(),
                        _ => answer
                            .push((
                                name,
                                Class::IN,
                                ttl,
                                Aaaa::new(if expected { address } else { other_address }),
                            ))
                            .unwrap(),
                    }
                    answer.finish()
                };
                server
                    .send_to(
                        &build(id.wrapping_add(7), Ttl::from_secs(60), false),
                        client,
                    )
                    .await
                    .unwrap();
                server
                    .send_to(&build(id, Ttl::from_secs(0), false), client)
                    .await
                    .unwrap();
                server
                    .send_to(&build(id, Ttl::from_secs(60), true), client)
                    .await
                    .unwrap();
            }
        };
        let (result, ()) = timeout(Duration::from_secs(3), async {
            tokio::join!(discover(client, peer, 29), exchange)
        })
        .await
        .unwrap();
        assert_eq!(result, Some(address.into()));
    }
}

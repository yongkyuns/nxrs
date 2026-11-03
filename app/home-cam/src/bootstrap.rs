//! Camera policy for a commissioned gateway; TLS and crypto remain in the HAL.
use crate::TlsIdentity;
use config::Store;
pub use enrollment::Commission;
use std::{
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream, UdpSocket},
    sync::Arc,
    time::{Duration, Instant},
};

const KEY: &str = "camera.bootstrap";
const REQUEST: Duration = Duration::from_secs(5);
const LEASE: Duration = Duration::from_secs(30);
const REFRESH: Duration = Duration::from_secs(15);
const LOOKUP: Duration = Duration::from_millis(500);
pub type TimeSource = Box<dyn FnMut(&tls::Identity) -> Result<tls::TimeLease, tls::Error>>;

pub fn load(store: &mut impl Store) -> Result<Option<Commission>, config::Error> {
    let mut bytes = [0; Commission::MAX_BYTES];
    store
        .get(KEY, &mut bytes)?
        .map(|n| {
            let value = bytes.get(..n).ok_or(config::Error::BufferTooSmall)?;
            Commission::decode(value).map_err(|_| config::Error::InvalidInput)
        })
        .transpose()
}
/// Commission before network startup, with no radio/camera resources active.
pub fn provision(store: &mut impl Store, bytes: &[u8]) -> Result<Commission, config::Error> {
    let commission = Commission::decode(bytes).map_err(|_| config::Error::InvalidInput)?;
    store.set(KEY, bytes)?;
    let mut observed = [0; Commission::MAX_BYTES];
    if store.get(KEY, &mut observed)? != Some(bytes.len())
        || observed.get(..bytes.len()) != Some(bytes)
    {
        return Err(config::Error::Io);
    }
    Ok(commission)
}

struct Bounded<'a> {
    connection: tls::Connection<'a>,
    until: Instant,
}
impl Bounded<'_> {
    fn retry<T>(
        &mut self,
        mut call: impl FnMut(&mut tls::Connection<'_>) -> io::Result<T>,
    ) -> io::Result<T> {
        loop {
            if Instant::now() >= self.until {
                return Err(io::ErrorKind::TimedOut.into());
            }
            match call(&mut self.connection) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    std::thread::sleep(Duration::from_millis(1))
                }
                value => return value,
            }
        }
    }
}
impl Read for Bounded<'_> {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        self.retry(|c| c.read(out))
    }
}
impl Write for Bounded<'_> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.retry(|c| c.write(bytes))
    }
    fn flush(&mut self) -> io::Result<()> {
        self.retry(|c| c.flush())
    }
}

struct Observation {
    time: tls::TimeLease,
    observed: Instant,
}
impl Observation {
    fn lease(&self) -> Result<tls::TimeLease, tls::Error> {
        Ok(self.time.clone())
    }
}
fn endpoints(commission: &Commission, until: Instant) -> Vec<SocketAddrV4> {
    let mut peers = Vec::with_capacity(4);
    let mut lookup = || -> io::Result<()> {
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))?;
        socket.set_broadcast(true)?;
        socket.set_nonblocking(true)?;
        let hint = commission.gateway;
        let destination = if hint.ip().is_loopback() {
            hint
        } else {
            SocketAddrV4::new(Ipv4Addr::BROADCAST, hint.port())
        };
        let query = enrollment::discovery::query(&commission.certificate_sha256);
        socket.send_to(&query, destination)?;
        let deadline = until.min(Instant::now() + LOOKUP);
        let mut next = Instant::now() + Duration::from_millis(30);
        let mut retries = 2;
        let mut packet = [0; enrollment::discovery::SIZE + 1];
        let mut received = 0;
        while Instant::now() < deadline && received < 16 && peers.len() < 3 {
            if retries > 0 && Instant::now() >= next {
                let _ = socket.send_to(&query, destination);
                retries -= 1;
                next += Duration::from_millis(30);
            }
            match socket.recv_from(&mut packet) {
                Ok((n, source)) => {
                    received += 1;
                    if let Some(peer) = enrollment::discovery::candidate(
                        &packet[..n],
                        source,
                        &commission.certificate_sha256,
                        hint.port(),
                    ) {
                        if !peers.contains(&peer) {
                            peers.push(peer);
                        }
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(error) => return Err(error),
            }
        }
        Ok(())
    };
    let _ = lookup(); // Discovery failure preserves the commissioned endpoint.
    if !commission.gateway.ip().is_unspecified() && !peers.contains(&commission.gateway) {
        peers.push(commission.gateway);
    }
    peers
}
fn connect_at<'a>(
    identity: &'a tls::Identity,
    pin: [u8; 32],
    peer: SocketAddrV4,
    budget: Duration,
    until: Instant,
) -> Result<Bounded<'a>, tls::Error> {
    let deadline = until.min(Instant::now() + budget);
    let socket =
        TcpStream::connect_timeout(&SocketAddr::V4(peer), budget).map_err(|_| tls::Error::Io)?;
    let remaining = until
        .checked_duration_since(Instant::now())
        .ok_or(tls::Error::Time)?;
    let mut connection = identity.connect_pinned(socket, pin, remaining)?;
    loop {
        if Instant::now() >= deadline {
            return Err(tls::Error::Time);
        }
        if connection.poll()? {
            return Ok(Bounded { connection, until });
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}
fn connect<'a>(
    identity: &'a tls::Identity,
    commission: &Commission,
    preferred: &mut Option<SocketAddrV4>,
    until: Instant,
) -> Result<Bounded<'a>, tls::Error> {
    if let Some(peer) = preferred.take() {
        let budget = until
            .checked_duration_since(Instant::now())
            .ok_or(tls::Error::Time)?
            / 2;
        if let Ok(connection) =
            connect_at(identity, commission.certificate_sha256, peer, budget, until)
        {
            *preferred = Some(peer);
            return Ok(connection);
        }
    }
    let peers = endpoints(commission, until);
    for (index, peer) in peers.iter().enumerate() {
        let remaining = until
            .checked_duration_since(Instant::now())
            .ok_or(tls::Error::Time)?;
        // A stalled/forged locator cannot consume the original endpoint's share.
        let budget = remaining / (peers.len() - index) as u32;
        if let Ok(connection) = connect_at(
            identity,
            commission.certificate_sha256,
            *peer,
            budget,
            until,
        ) {
            // Volatile locator only; every reuse still authenticates the TLS pin.
            *preferred = Some(*peer);
            return Ok(connection);
        }
    }
    Err(tls::Error::Io)
}
fn exchange(
    identity: &tls::Identity,
    commission: &Commission,
    preferred: &mut Option<SocketAddrV4>,
    csr: &[u8],
) -> Result<(Observation, Vec<u8>), tls::Error> {
    let started = Instant::now();
    let until = started + REQUEST;
    let mut transport = connect(identity, commission, preferred, until)?;
    enrollment::request(&mut transport, &commission.token, csr).map_err(|_| tls::Error::Io)?;
    let (epoch, bytes) = enrollment::read_response(&mut transport).map_err(|_| tls::Error::Io)?;
    let observed = Instant::now();
    let uncertainty = observed.duration_since(started);
    if uncertainty >= REQUEST {
        return Err(tls::Error::Time);
    }
    let observation = Observation {
        time: tls::TimeLease::from_trusted_with_uncertainty(epoch, LEASE, observed, uncertainty)?,
        observed,
    };
    Ok((observation, bytes))
}

pub fn initialize(commission: Arc<Commission>) -> Result<TlsIdentity<TimeSource>, crate::Error> {
    let mut identity = tls::open().map_err(crate::Error::Tls)?;
    let mut csr = [0; enrollment::MAX_CSR];
    let size = identity
        .request("camera", &mut csr)
        .map_err(crate::Error::Tls)?;
    let mut preferred = None;
    let (mut time, bytes) = exchange(&identity, &commission, &mut preferred, &csr[..size])
        .map_err(crate::Error::Tls)?;
    let certificates = enrollment::Enrollment::decode(&bytes)
        .map_err(|_| crate::Error::Tls(tls::Error::InvalidInput))?;
    if certificates.own_uri != commission.own_uri()
        || certificates.peer_uri != commission.peer_uri()
    {
        return Err(crate::Error::Tls(tls::Error::Authentication));
    }
    let clock = move |identity: &tls::Identity| {
        if time.observed.elapsed() >= REFRESH {
            let (mut fresh, bytes) = exchange(identity, &commission, &mut preferred, &[])?;
            if !bytes.is_empty() {
                return Err(tls::Error::Time);
            }
            fresh.time.advance_from(&time.time)?;
            time = fresh;
        }
        time.lease()
    };
    Ok(TlsIdentity {
        identity,
        policy: tls::CertificatePolicy {
            issuer_sha256: certificates.issuer_sha256,
            own_uri: certificates.own_uri,
            peer_uri: certificates.peer_uri,
        },
        own: certificates.own,
        issuer: certificates.issuer,
        approved_peer: certificates.approved_peer,
        revocations: certificates.revocations,
        time: Box::new(clock),
    })
}

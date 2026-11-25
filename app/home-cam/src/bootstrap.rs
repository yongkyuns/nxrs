//! Camera policy for a commissioned gateway; TLS and crypto remain in the HAL.
use crate::{RefreshStep, TimeChannel, TlsIdentity};
use settings::Store;
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
const CLOCK_CHANNEL_LIFETIME: Duration = Duration::from_secs(60 * 60);
const RENEW_AFTER: Duration = Duration::from_secs(45 * 60);
const RENEW_RETRY: Duration = Duration::from_secs(30);
pub type TimeSource = Box<
    dyn for<'id> FnMut(
        &'id tls::Identity,
        TimeChannel<'id, '_>,
        RefreshStep<'_>,
    ) -> Result<tls::TimeLease, tls::Error>,
>;

pub fn load(store: &mut impl Store) -> Result<Option<Commission>, settings::Error> {
    let mut bytes = [0; Commission::MAX_BYTES];
    store
        .get(KEY, &mut bytes)?
        .map(|n| {
            let value = bytes.get(..n).ok_or(settings::Error::BufferTooSmall)?;
            Commission::decode(value).map_err(|_| settings::Error::InvalidInput)
        })
        .transpose()
}
/// Commission before network startup, with no radio/camera resources active.
pub fn provision(store: &mut impl Store, bytes: &[u8]) -> Result<Commission, settings::Error> {
    let commission = Commission::decode(bytes).map_err(|_| settings::Error::InvalidInput)?;
    store.set(KEY, bytes)?;
    let mut observed = [0; Commission::MAX_BYTES];
    if store.get(KEY, &mut observed)? != Some(bytes.len())
        || observed.get(..bytes.len()) != Some(bytes)
    {
        return Err(settings::Error::Io);
    }
    Ok(commission)
}

struct Bounded<'id, 'borrow> {
    connection: &'borrow mut tls::Connection<'id>,
    until: Instant,
    step: RefreshStep<'borrow>,
}
impl Bounded<'_, '_> {
    fn retry<T>(
        &mut self,
        mut call: impl FnMut(&mut tls::Connection<'_>) -> io::Result<T>,
    ) -> io::Result<T> {
        loop {
            if Instant::now() >= self.until {
                return Err(io::ErrorKind::TimedOut.into());
            }
            match call(self.connection) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    (self.step)().map_err(|_| io::ErrorKind::ConnectionAborted)?
                }
                value => return value,
            }
        }
    }
}
impl Read for Bounded<'_, '_> {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        self.retry(|c| c.read(out))
    }
}
impl Write for Bounded<'_, '_> {
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
fn endpoints(commission: &Commission, until: Instant, step: RefreshStep<'_>) -> Vec<SocketAddrV4> {
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
                    step().map_err(|_| io::ErrorKind::Interrupted)?;
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
#[derive(Clone, Copy)]
struct Connect {
    until: Instant,
    live: bool,
    retain: bool,
}
fn connect_at<'a>(
    identity: &'a tls::Identity,
    pin: [u8; 32],
    peer: SocketAddrV4,
    budget: Duration,
    options: Connect,
    step: RefreshStep<'_>,
) -> Result<tls::Connection<'a>, tls::Error> {
    let deadline = options.until.min(Instant::now() + budget);
    // One TCP connect attempt must not monopolize the video owner.
    let tcp_budget = if options.live {
        Duration::from_millis(20)
    } else {
        REQUEST
    };
    let socket = TcpStream::connect_timeout(&SocketAddr::V4(peer), budget.min(tcp_budget))
        .map_err(|_| tls::Error::Io)?;
    let remaining = options
        .until
        .checked_duration_since(Instant::now())
        .ok_or(tls::Error::Time)?;
    let lifetime = if options.retain {
        CLOCK_CHANNEL_LIFETIME
    } else {
        remaining
    };
    let mut connection = identity.connect_pinned(socket, pin, lifetime)?;
    loop {
        if Instant::now() >= deadline {
            return Err(tls::Error::Time);
        }
        if connection.poll()? {
            return Ok(connection);
        }
        step()?;
    }
}
fn connect<'a>(
    identity: &'a tls::Identity,
    commission: &Commission,
    preferred: &mut Option<SocketAddrV4>,
    options: Connect,
    step: RefreshStep<'_>,
) -> Result<tls::Connection<'a>, tls::Error> {
    if let Some(peer) = preferred.take() {
        let budget = options
            .until
            .checked_duration_since(Instant::now())
            .ok_or(tls::Error::Time)?
            / 2;
        if let Ok(connection) = connect_at(
            identity,
            commission.certificate_sha256,
            peer,
            budget,
            options,
            step,
        ) {
            *preferred = Some(peer);
            return Ok(connection);
        }
    }
    step()?;
    let peers = endpoints(commission, options.until, step);
    for (index, peer) in peers.iter().enumerate() {
        step()?;
        let remaining = options
            .until
            .checked_duration_since(Instant::now())
            .ok_or(tls::Error::Time)?;
        // A stalled/forged locator cannot consume the original endpoint's share.
        let budget = remaining / (peers.len() - index) as u32;
        if let Ok(connection) = connect_at(
            identity,
            commission.certificate_sha256,
            *peer,
            budget,
            options,
            step,
        ) {
            // Volatile locator only; every reuse still authenticates the TLS pin.
            *preferred = Some(*peer);
            return Ok(connection);
        }
    }
    Err(tls::Error::Io)
}
#[derive(Clone, Copy)]
enum Request<'a> {
    Time,
    Enroll(&'a [u8]),
    Renew(&'a [u8]),
}

fn exchange<'id>(
    identity: &'id tls::Identity,
    commission: &Commission,
    preferred: &mut Option<SocketAddrV4>,
    kind: Request<'_>,
    mut channel: TimeChannel<'id, '_>,
    step: RefreshStep<'_>,
) -> Result<(Observation, Vec<u8>), tls::Error> {
    let started = Instant::now();
    if let Some(slot) = channel.as_deref_mut() {
        if let Some(mut connection) = slot.take() {
            if matches!(kind, Request::Time) {
                match request(&mut connection, commission, kind, started, step) {
                    Ok(response) => {
                        *slot = Some(connection);
                        return Ok(response);
                    }
                    // Older gateways close after a reply. Reconnect under
                    // the same pin and the original authenticated lease.
                    Err(tls::Error::Io) => step()?,
                    Err(error) => return Err(error),
                }
            }
        }
    }
    let options = Connect {
        until: started + REQUEST,
        live: channel.is_some(),
        retain: !matches!(kind, Request::Enroll(_)),
    };
    let mut connection = connect(identity, commission, preferred, options, step)?;
    let response = request(&mut connection, commission, kind, started, step)?;
    if let Some(slot) = channel {
        *slot = Some(connection);
    }
    Ok(response)
}
fn request(
    connection: &mut tls::Connection<'_>,
    commission: &Commission,
    kind: Request<'_>,
    started: Instant,
    step: RefreshStep<'_>,
) -> Result<(Observation, Vec<u8>), tls::Error> {
    let mut transport = Bounded {
        connection,
        until: started + REQUEST,
        step,
    };
    match kind {
        Request::Time => enrollment::request(&mut transport, &commission.token, &[]),
        Request::Enroll(csr) => enrollment::request(&mut transport, &commission.token, csr),
        Request::Renew(csr) => enrollment::renew(&mut transport, &commission.token, csr),
    }
    .map_err(|_| tls::Error::Io)?;
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

fn decode(commission: &Commission, bytes: &[u8]) -> Result<enrollment::Enrollment, tls::Error> {
    let certificates =
        enrollment::Enrollment::decode(bytes).map_err(|_| tls::Error::InvalidInput)?;
    if certificates.own_uri != commission.own_uri()
        || certificates.peer_uri != commission.peer_uri()
    {
        return Err(tls::Error::Authentication);
    }
    Ok(certificates)
}

struct Clock {
    commission: Arc<Commission>,
    preferred: Option<SocketAddrV4>,
    time: Observation,
    renew_at: Instant,
}
impl Clock {
    fn refresh<'id>(
        &mut self,
        identity: &'id tls::Identity,
        mut channel: TimeChannel<'id, '_>,
        step: RefreshStep<'_>,
    ) -> Result<tls::TimeLease, tls::Error> {
        if channel.is_some() && Instant::now() >= self.renew_at {
            match self.renew(identity, channel.as_deref_mut(), step) {
                Ok(()) => self.renew_at = self.time.observed + RENEW_AFTER,
                Err(error) => {
                    eprintln!("CAMERA_GRANT_RETRY error={error:?}");
                    self.renew_at = Instant::now() + RENEW_RETRY;
                }
            }
        }
        if channel.as_ref().is_some_and(|slot| slot.is_none())
            || self.time.observed.elapsed() >= REFRESH
        {
            let (mut fresh, bytes) = exchange(
                identity,
                &self.commission,
                &mut self.preferred,
                Request::Time,
                channel,
                step,
            )?;
            if !bytes.is_empty() {
                return Err(tls::Error::Time);
            }
            fresh.time.advance_from(&self.time.time)?;
            self.time = fresh;
        }
        self.time.lease()
    }

    fn renew<'id>(
        &mut self,
        identity: &'id tls::Identity,
        channel: TimeChannel<'id, '_>,
        step: RefreshStep<'_>,
    ) -> Result<(), tls::Error> {
        step()?;
        let mut csr = [0; enrollment::MAX_CSR];
        let size = identity.request("camera", &mut csr)?;
        let (mut fresh, bytes) = exchange(
            identity,
            &self.commission,
            &mut self.preferred,
            Request::Renew(&csr[..size]),
            channel,
            step,
        )?;
        let certificates = decode(&self.commission, &bytes)?;
        fresh.time.advance_from(&self.time.time)?;
        step()?;
        identity.renew(
            tls::Certificates {
                policy: &tls::CertificatePolicy {
                    issuer_sha256: certificates.issuer_sha256,
                    own_uri: certificates.own_uri,
                    peer_uri: certificates.peer_uri,
                },
                own: &certificates.own,
                issuer: &certificates.issuer,
                approved_peer: &certificates.approved_peer,
                revocations: &certificates.revocations,
            },
            &fresh.time,
            step,
        )?;
        self.time = fresh;
        Ok(())
    }
}

pub fn initialize(commission: Arc<Commission>) -> Result<TlsIdentity<TimeSource>, crate::Error> {
    let identity = tls::open().map_err(crate::Error::Tls)?;
    let mut csr = [0; enrollment::MAX_CSR];
    let size = identity
        .request("camera", &mut csr)
        .map_err(crate::Error::Tls)?;
    let mut preferred = None;
    let (time, bytes) = exchange(
        &identity,
        &commission,
        &mut preferred,
        Request::Enroll(&csr[..size]),
        None,
        &mut || {
            std::thread::sleep(Duration::from_millis(1));
            Ok(())
        },
    )
    .map_err(crate::Error::Tls)?;
    let certificates = decode(&commission, &bytes).map_err(crate::Error::Tls)?;
    let mut clock = Clock {
        commission,
        preferred,
        renew_at: time.observed + RENEW_AFTER,
        time,
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
        time: Box::new(move |identity, channel, step| clock.refresh(identity, channel, step)),
    })
}

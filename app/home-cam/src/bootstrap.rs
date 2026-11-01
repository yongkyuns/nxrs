//! Camera policy for a commissioned gateway; TLS and crypto remain in the HAL.
use crate::TlsIdentity;
use config::Store;
pub use enrollment::Commission;
use std::{
    io::{self, Read, Write},
    net::{SocketAddr, TcpStream},
    sync::Arc,
    time::{Duration, Instant},
};

const KEY: &str = "camera.bootstrap";
const REQUEST: Duration = Duration::from_secs(5);
const LEASE: Duration = Duration::from_secs(30);
const REFRESH: Duration = Duration::from_secs(15);
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
fn exchange(
    identity: &tls::Identity,
    commission: &Commission,
    csr: &[u8],
) -> Result<(Observation, Vec<u8>), tls::Error> {
    let started = Instant::now();
    let until = started + REQUEST;
    let socket = TcpStream::connect_timeout(&SocketAddr::V4(commission.gateway), REQUEST)
        .map_err(|_| tls::Error::Io)?;
    let remaining = until
        .checked_duration_since(Instant::now())
        .ok_or(tls::Error::Time)?;
    let mut transport = Bounded {
        connection: identity.connect_pinned(socket, commission.certificate_sha256, remaining)?,
        until,
    };
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
    let (mut time, bytes) =
        exchange(&identity, &commission, &csr[..size]).map_err(crate::Error::Tls)?;
    let certificates = enrollment::Enrollment::decode(&bytes)
        .map_err(|_| crate::Error::Tls(tls::Error::InvalidInput))?;
    if certificates.own_uri != commission.own_uri()
        || certificates.peer_uri != commission.peer_uri()
    {
        return Err(crate::Error::Tls(tls::Error::Authentication));
    }
    let clock = move |identity: &tls::Identity| {
        if time.observed.elapsed() >= REFRESH {
            let (mut fresh, bytes) = exchange(identity, &commission, &[])?;
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

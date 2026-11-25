//! Camera-specific authenticated transport. This host gateway does not select a
//! device HAL or a system trust store. Rustls performs certificate/signature/CRL
//! verification; the additional checks bind the approved enrollment and lease.
use crate::{
    config::{CameraTls, Config},
    Error, Result,
};
use rustls::pki_types::{pem::PemObject, PrivatePkcs8KeyDer};
use rustls::{
    client::{
        danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
        Resumption, WebPkiServerVerifier,
    },
    pki_types::{CertificateDer, CertificateRevocationListDer, ServerName, UnixTime},
    server::WebPkiClientVerifier,
    ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme,
};
use sha2::{Digest, Sha256};
use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpStream,
    time::{sleep_until, Instant},
};
use tokio_rustls::TlsConnector;
use x509_parser::{extensions::GeneralName, prelude::*};
use zeroize::Zeroizing;

pub(crate) trait CameraIo: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> CameraIo for T {}
pub(crate) type Socket = Box<dyn CameraIo>;

fn require(condition: bool) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error::Authentication)
    }
}
fn clock() -> Result<Duration> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Authentication)
}
fn observe_clock(previous: &mut u64, now: u64) -> Result<()> {
    require(now >= *previous)?;
    *previous = now;
    Ok(())
}
fn item<T: PemObject>(path: &Path, label: &str, limit: u64) -> Result<T> {
    let bytes =
        Zeroizing::new(crate::config::private_read(path, limit).map_err(|_| Error::Configuration)?);
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| Error::Authentication)?
        .trim_ascii();
    require(
        text.is_ascii()
            && text.starts_with(&format!("-----BEGIN {label}-----"))
            && text.ends_with(&format!("-----END {label}-----"))
            && text.matches("-----BEGIN ").count() == 1
            && text.matches("-----END ").count() == 1,
    )?;
    T::from_pem_slice(&bytes).map_err(|_| Error::Authentication)
}
fn cert(path: &Path) -> Result<CertificateDer<'static>> {
    item(path, "CERTIFICATE", 16_384)
}

pub(crate) fn check_public_key(path: &Path, key: &[u8]) -> Result<()> {
    let certificate = cert(path)?;
    let (rest, parsed) = parse_x509_certificate(&certificate).map_err(|_| Error::Authentication)?;
    require(rest.is_empty() && parsed.public_key().subject_public_key.data.as_ref() == key)
}

fn parsed<'a>(der: &'a [u8], now: u64) -> Result<X509Certificate<'a>> {
    let (rest, cert) = parse_x509_certificate(der).map_err(|_| Error::Authentication)?;
    require(
        rest.is_empty()
            && cert.validity().not_before.timestamp() >= 0
            && cert.validity().not_before.timestamp() as u64 <= now
            && cert.validity().not_after.timestamp() > 0
            && now < cert.validity().not_after.timestamp() as u64,
    )?;
    let algorithm = &cert.public_key().algorithm;
    require(
        algorithm.algorithm.to_id_string() == "1.2.840.10045.2.1"
            && algorithm
                .parameters
                .as_ref()
                .and_then(|p| p.as_oid().ok())
                .is_some_and(|oid| oid.to_id_string() == "1.2.840.10045.3.1.7")
            && cert.signature_algorithm.algorithm.to_id_string() == "1.2.840.10045.4.3.2",
    )?;
    Ok(cert)
}
fn leaf(
    der: &[u8],
    config: &Config,
    enrollment: &CameraTls,
    server: bool,
    now: u64,
) -> Result<(u64, [u8; 32])> {
    let cert = parsed(der, now)?;
    let basic = cert
        .basic_constraints()
        .map_err(|_| Error::Authentication)?
        .ok_or(Error::Authentication)?;
    let usage = cert
        .key_usage()
        .map_err(|_| Error::Authentication)?
        .ok_or(Error::Authentication)?;
    let eku = cert
        .extended_key_usage()
        .map_err(|_| Error::Authentication)?
        .ok_or(Error::Authentication)?;
    require(
        basic.critical
            && !basic.value.ca
            && usage.critical
            && usage.value.flags == 1
            && eku.value.server_auth == server
            && eku.value.client_auth != server
            && !eku.value.any
            && !eku.value.code_signing
            && !eku.value.email_protection
            && !eku.value.time_stamping
            && !eku.value.ocsp_signing
            && eku.value.other.is_empty(),
    )?;
    let san = cert
        .subject_alternative_name()
        .map_err(|_| Error::Authentication)?
        .ok_or(Error::Authentication)?;
    let expected = if server {
        format!(
            "urn:nxrs:camera:{}:{}",
            enrollment.site_id, enrollment.camera_id
        )
    } else {
        format!(
            "urn:nxrs:ingest:{}:{}:{}",
            enrollment.site_id, enrollment.gateway_id, enrollment.camera_id
        )
    };
    let names = &san.value.general_names;
    require(names.len() == if server { 2 } else { 1 })?;
    require(
        names
            .iter()
            .any(|name| matches!(name, GeneralName::URI(uri) if *uri==expected)),
    )?;
    if server {
        require(names.iter().any(
            |name| matches!(name, GeneralName::IPAddress(ip) if *ip==config.camera.ip().octets()),
        ))?;
    }
    Ok((
        cert.validity().not_after.timestamp() as u64,
        Sha256::digest(cert.public_key().raw).into(),
    ))
}

/// This verifier adds exact enrolled-leaf approval while delegating *all* chain,
/// IP, certificate lifetime, CRL and TLS handshake signature checks to Rustls.
#[derive(Debug)]
struct PinnedVerifier {
    inner: Arc<WebPkiServerVerifier>,
    approved: CertificateDer<'static>,
}
impl ServerCertVerifier for PinnedVerifier {
    fn verify_server_cert(
        &self,
        end: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        name: &ServerName<'_>,
        ocsp: &[u8],
        now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        if end.as_ref() != self.approved.as_ref() || intermediates.len() > 1 {
            return Err(rustls::Error::InvalidCertificate(
                rustls::CertificateError::ApplicationVerificationFailure,
            ));
        }
        self.inner
            .verify_server_cert(end, intermediates, name, ocsp, now)
    }
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}
struct Authenticated {
    connector: TlsConnector,
    name: ServerName<'static>,
    keys: [[u8; 32]; 3],
    until: u64,
    deadline: Instant,
}
struct Authorization {
    current: Arc<Authenticated>,
    observed_time: u64,
}
impl Authorization {
    fn observe_clock(&mut self) -> Result<u64> {
        let now = clock()?.as_secs();
        observe_clock(&mut self.observed_time, now)?;
        Ok(now)
    }

    fn authorized(&mut self) -> Result<Arc<Authenticated>> {
        let now = self.observe_clock()?;
        require(now < self.current.until && Instant::now() < self.current.deadline)?;
        Ok(self.current.clone())
    }
}
#[derive(Clone)]
pub(crate) struct Prepared {
    tls: Option<Arc<Mutex<Authorization>>>,
}
impl Prepared {
    pub(crate) fn load(config: &Config) -> Result<Self> {
        let Some(enrollment) = &config.camera_tls else {
            return Ok(Self { tls: None });
        };
        let observed_at = Instant::now();
        let now = clock()?;
        let ca = cert(&enrollment.ca_certificate_file)?;
        let issuer_key: [u8; 32] = Sha256::digest(&ca).into();
        let digest: String = issuer_key.iter().map(|b| format!("{b:02x}")).collect();
        require(digest == enrollment.issuer_sha256)?;
        let root = parsed(&ca, now.as_secs())?;
        let basic = root
            .basic_constraints()
            .map_err(|_| Error::Authentication)?
            .ok_or(Error::Authentication)?;
        let usage = root
            .key_usage()
            .map_err(|_| Error::Authentication)?
            .ok_or(Error::Authentication)?;
        require(
            basic.critical
                && basic.value.ca
                && basic.value.path_len_constraint == Some(0)
                && usage.critical
                && usage.value.key_cert_sign()
                && usage.value.crl_sign()
                && root.issuer() == root.subject(),
        )?;
        let camera = cert(&enrollment.camera_certificate_file)?;
        let client = cert(&enrollment.client_certificate_file)?;
        let (camera_until, camera_key) = leaf(&camera, config, enrollment, true, now.as_secs())?;
        let (client_until, client_key) = leaf(&client, config, enrollment, false, now.as_secs())?;
        let mut until = camera_until
            .min(client_until)
            .min(root.validity().not_after.timestamp() as u64);
        let crl: CertificateRevocationListDer<'static> =
            item(&enrollment.crl_file, "X509 CRL", 131_072)?;
        let (rest, revocations) = parse_x509_crl(&crl).map_err(|_| Error::Authentication)?;
        let next = revocations
            .next_update()
            .ok_or(Error::Authentication)?
            .timestamp();
        let last = revocations.last_update().timestamp();
        require(
            rest.is_empty()
                && revocations.issuer() == root.subject()
                && last >= 0
                && last as u64 <= now.as_secs()
                && next > last
                && next - last <= 86_400
                && next as u64 > now.as_secs()
                && revocations.iter_revoked_certificates().count() <= 512
                && revocations.crl_number().is_some_and(|number| {
                    number.to_string() == enrollment.enrollment_generation.to_string()
                }),
        )?;
        until = until.min(next as u64);
        let key: PrivatePkcs8KeyDer<'static> =
            item(&enrollment.client_key_file, "PRIVATE KEY", 32_768)?;
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut roots = RootCertStore::empty();
        roots.add(ca).map_err(|_| Error::Authentication)?;
        let roots = Arc::new(roots);
        let verifier = WebPkiServerVerifier::builder_with_provider(roots.clone(), provider.clone())
            .with_crls([crl.clone()])
            .enforce_revocation_expiration()
            .build()
            .map_err(|_| Error::Authentication)?;
        let client_verifier = WebPkiClientVerifier::builder_with_provider(roots, provider.clone())
            .with_crls([crl])
            .enforce_revocation_expiration()
            .build()
            .map_err(|_| Error::Authentication)?;
        let name: ServerName<'static> = ServerName::IpAddress((*config.camera.ip()).into());
        let at = UnixTime::since_unix_epoch(now);
        verifier
            .verify_server_cert(&camera, &[], &name, &[], at)
            .map_err(|_| Error::Authentication)?;
        client_verifier
            .verify_client_cert(&client, &[], at)
            .map_err(|_| Error::Authentication)?;
        let mut tls = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|_| Error::Authentication)?
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(PinnedVerifier {
                inner: verifier,
                approved: camera,
            }))
            .with_client_auth_cert(vec![client], key.into())
            .map_err(|_| Error::Authentication)?;
        tls.resumption = Resumption::disabled();
        tls.enable_early_data = false;
        tls.require_ems = true;
        // Match the camera's bounded Mbed TLS receive record buffer.
        tls.max_fragment_size = Some(4096);
        let remaining = Duration::from_secs(until)
            .checked_sub(now)
            .ok_or(Error::Authentication)?;
        let deadline = observed_at
            .checked_add(remaining)
            .ok_or(Error::Authentication)?;
        Ok(Self {
            tls: Some(Arc::new(Mutex::new(Authorization {
                current: Arc::new(Authenticated {
                    connector: TlsConnector::from(Arc::new(tls)),
                    name,
                    keys: [issuer_key, camera_key, client_key],
                    until,
                    deadline,
                }),
                observed_time: now.as_secs(),
            }))),
        })
    }
    pub(crate) async fn connect(&self, socket: TcpStream) -> Result<Socket> {
        match &self.tls {
            None => Ok(Box::new(socket)),
            Some(authorization) => {
                let tls = authorization
                    .lock()
                    .map_err(|_| Error::Authentication)?
                    .authorized()?;
                let stream = tls
                    .connector
                    .connect_with(tls.name.clone(), socket, |connection| {
                        connection.set_buffer_limit(Some(16_384))
                    })
                    .await
                    .map_err(|error| {
                        if error.kind() == std::io::ErrorKind::InvalidData
                            || error
                                .get_ref()
                                .is_some_and(|source| source.is::<rustls::Error>())
                        {
                            Error::Authentication
                        } else {
                            Error::Io
                        }
                    })?;
                Ok(Box::new(stream))
            }
        }
    }
    /// Reauthorize the existing transport with fresh credentials for the same
    /// peer keys, issuer and address. New handshakes use the updated config.
    pub(crate) fn renew(&self, candidate: &Self) -> Result<()> {
        let live = self.tls.as_ref().ok_or(Error::Authentication)?;
        let candidate = candidate
            .tls
            .as_ref()
            .ok_or(Error::Authentication)?
            .lock()
            .map_err(|_| Error::Authentication)?
            .authorized()?;
        let mut live = live.lock().map_err(|_| Error::Authentication)?;
        require(live.current.keys == candidate.keys && live.current.name == candidate.name)?;
        require(live.observe_clock()? < candidate.until && Instant::now() < candidate.deadline)?;
        live.current = candidate;
        Ok(())
    }

    /// Enforce absolute credential/CRL expiration as a monotonic maximum lease;
    /// observe forward/backward wall-clock jumps within one second. A same-key
    /// renewal replaces this deadline without restarting the source or encoder.
    pub(crate) async fn guard(&self) -> Error {
        let Some(authorization) = &self.tls else {
            return std::future::pending().await;
        };
        loop {
            let Ok(tls) = authorization
                .lock()
                .map_err(|_| Error::Authentication)
                .and_then(|mut state| state.authorized())
            else {
                return Error::Authentication;
            };
            sleep_until(tls.deadline.min(Instant::now() + Duration::from_secs(1))).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clock_high_water_survives_replacement_of_a_guard_future() {
        let mut previous = 100;
        for now in [100, 110, 110, 120] {
            assert_eq!(observe_clock(&mut previous, now), Ok(()));
        }
        assert_eq!(
            observe_clock(&mut previous, 115),
            Err(Error::Authentication)
        );
        assert_eq!(
            observe_clock(&mut previous, 100),
            Err(Error::Authentication)
        );
        assert_eq!(previous, 120);
    }
}

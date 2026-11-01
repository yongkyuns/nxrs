//! A per-camera pinned TLS issuer/time endpoint, owned by the Rust ingest process.
use crate::{
    config::{self, CameraTls, Config},
    runner, Error, Result,
};
use enrollment::{Commission, Enrollment};
use rcgen::{
    BasicConstraints, CertificateParams, CertificateRevocationListParams,
    CertificateSigningRequestParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyIdMethod,
    KeyPair, KeyUsagePurpose, SanType, SerialNumber, PKCS_ECDSA_P256_SHA256,
};
use rustls::{
    pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer},
    ServerConfig,
};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;
use time::OffsetDateTime;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::watch,
    task::JoinHandle,
    time::timeout,
};
use tokio_rustls::TlsAcceptor;
use zeroize::Zeroizing;

fn configuration<T>(value: std::result::Result<T, impl std::fmt::Debug>) -> Result<T> {
    value.map_err(|_| Error::Configuration)
}
fn now() -> Result<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|v| v.as_secs())
        .map_err(|_| Error::Authentication)
}

#[cfg(unix)]
fn private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    match builder.create(path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(_) => return Err(Error::Io),
    }
    validate_dir(path)
}
#[cfg(unix)]
fn validate_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir() || metadata.mode() & 0o077 != 0 {
        return Err(Error::Configuration);
    }
    Ok(())
}
#[cfg(not(unix))]
fn private_dir(_: &Path) -> Result<()> {
    Err(Error::Configuration)
}
#[cfg(not(unix))]
fn validate_dir(_: &Path) -> Result<()> {
    Err(Error::Configuration)
}
#[cfg(unix)]
fn write_new(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}
#[cfg(not(unix))]
fn write_new(_: &Path, _: &[u8]) -> Result<()> {
    Err(Error::Configuration)
}

fn leaf(uri: &str, epoch: u64, serial: u64, client: bool) -> Result<CertificateParams> {
    let mut params = configuration(CertificateParams::new(Vec::<String>::new()))?;
    params.not_before = configuration(OffsetDateTime::from_unix_timestamp(epoch as i64 - 60))?;
    params.not_after = configuration(OffsetDateTime::from_unix_timestamp(epoch as i64 + 3600))?;
    params.serial_number = Some(SerialNumber::from(serial));
    params.is_ca = IsCa::ExplicitNoCa;
    params
        .distinguished_name
        .push(DnType::CommonName, "Nxrs camera enrollment");
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![if client {
        ExtendedKeyUsagePurpose::ClientAuth
    } else {
        ExtendedKeyUsagePurpose::ServerAuth
    }];
    params.subject_alt_names = vec![SanType::URI(configuration(uri.try_into())?)];
    params.use_authority_key_identifier_extension = true;
    Ok(params)
}

pub fn initialize(config: &Config) -> Result<()> {
    config.validate()?;
    let settings = config.bootstrap.as_ref().ok_or(Error::Configuration)?;
    private_dir(&settings.directory)?;
    if fs::read_dir(&settings.directory)?.next().is_some() {
        return Err(Error::Configuration);
    }
    let names = settings;
    let epoch = now()?;
    let ca_key = configuration(KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256))?;
    let mut ca = configuration(CertificateParams::new(Vec::<String>::new()))?;
    ca.distinguished_name
        .push(DnType::CommonName, "Nxrs enrollment authority");
    ca.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    ca.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    ca.not_before = configuration(OffsetDateTime::from_unix_timestamp(epoch as i64 - 60))?;
    ca.not_after = configuration(OffsetDateTime::from_unix_timestamp(
        epoch as i64 + 10 * 365 * 86400,
    ))?;
    let ca_cert = configuration(ca.self_signed(&ca_key))?;
    let bootstrap_key = configuration(KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256))?;
    let mut bootstrap = leaf("urn:nxrs:bootstrap", epoch, 1, false)?;
    bootstrap.not_after = ca.not_after;
    let bootstrap_cert = configuration(bootstrap.self_signed(&bootstrap_key))?;
    let mut token = Zeroizing::new([0; 32]);
    configuration(getrandom::fill(&mut *token))?;
    let commission = Commission {
        gateway: settings.listen,
        certificate_sha256: Sha256::digest(bootstrap_cert.der()).into(),
        token: *token,
        site: names.site_id.clone(),
        camera: names.camera_id.clone(),
        gateway_id: names.gateway_id.clone(),
    };
    for (name, bytes) in [
        ("ca.pem", ca_cert.pem().into_bytes()),
        ("ca.key", ca_key.serialize_pem().into_bytes()),
        ("bootstrap.pem", bootstrap_cert.pem().into_bytes()),
        ("bootstrap.key", bootstrap_key.serialize_pem().into_bytes()),
        ("token", token.to_vec()),
        ("generation", b"0\n".to_vec()),
        ("commission.bin", configuration(commission.encode())?),
    ] {
        let bytes = Zeroizing::new(bytes);
        write_new(&settings.directory.join(name), &bytes)?;
    }
    Ok(())
}

struct Authority {
    config: Config,
    directory: PathBuf,
    issuer: Issuer<'static, KeyPair>,
    ca_der: Vec<u8>,
    token: Zeroizing<Vec<u8>>,
    generation: u64,
    clock: u64,
    active: Option<(watch::Sender<bool>, JoinHandle<Result<()>>)>,
    grant_directory: Option<PathBuf>,
}
struct Staging(PathBuf);
impl Drop for Staging {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
impl Authority {
    fn load(config: Config) -> Result<(Self, TlsAcceptor)> {
        config.validate()?;
        let directory = config
            .bootstrap
            .as_ref()
            .ok_or(Error::Configuration)?
            .directory
            .clone();
        validate_dir(&directory)?;
        let ca_pem = config::private_read(&directory.join("ca.pem"), 4096)?;
        let ca_key = Zeroizing::new(config::private_read(&directory.join("ca.key"), 4096)?);
        let issuer = configuration(Issuer::from_ca_cert_pem(
            configuration(std::str::from_utf8(&ca_pem))?,
            configuration(KeyPair::from_pem(configuration(std::str::from_utf8(
                &ca_key,
            ))?))?,
        ))?;
        let ca_der = configuration(CertificateDer::from_pem_slice(&ca_pem))?
            .as_ref()
            .to_vec();
        let certificate = configuration(CertificateDer::from_pem_slice(&config::private_read(
            &directory.join("bootstrap.pem"),
            4096,
        )?))?;
        let key = Zeroizing::new(config::private_read(
            &directory.join("bootstrap.key"),
            4096,
        )?);
        let private = configuration(PrivateKeyDer::from_pem_slice(&key))?;
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut server = configuration(
            ServerConfig::builder_with_provider(provider)
                .with_protocol_versions(&[&rustls::version::TLS13]),
        )?
        .with_no_client_auth()
        .with_single_cert(vec![certificate], private)
        .map_err(|_| Error::Configuration)?;
        server.max_fragment_size = Some(4096);
        server.send_tls13_tickets = 0;
        server.session_storage = Arc::new(rustls::server::NoServerSessionStorage {});
        let token = Zeroizing::new(config::private_read(&directory.join("token"), 32)?);
        if token.len() != 32 || token.iter().all(|b| *b == 0) {
            return Err(Error::Configuration);
        }
        let generation = configuration(std::str::from_utf8(&config::private_read(
            &directory.join("generation"),
            32,
        )?))?
        .trim()
        .parse()
        .map_err(|_| Error::Configuration)?;
        Ok((
            Self {
                config,
                directory,
                issuer,
                ca_der,
                token,
                generation,
                clock: now()?,
                active: None,
                grant_directory: None,
            },
            TlsAcceptor::from(Arc::new(server)),
        ))
    }
    async fn retire(&mut self) -> Result<()> {
        if let Some((stop, task)) = self.active.as_mut() {
            let _ = stop.send(true);
            let result = task.await;
            self.active = None;
            if matches!(result, Err(_) | Ok(Err(Error::Cleanup))) {
                return Err(Error::Cleanup);
            }
        }
        Ok(())
    }
    fn config_for(&self, generation: u64) -> Result<Config> {
        let directory = self.directory.join(format!("grant-{generation}"));
        self.config_in(generation, &directory)
    }
    fn config_in(&self, generation: u64, directory: &Path) -> Result<Config> {
        let mut config = self.config.clone();
        let ip: [u8; 4] = config::private_read(&directory.join("camera.ip"), 4)?
            .try_into()
            .map_err(|_| Error::Configuration)?;
        config.camera.set_ip(ip.into());
        config.bootstrap = None;
        let names = self.config.bootstrap.as_ref().ok_or(Error::Configuration)?;
        config.camera_tls = Some(CameraTls {
            site_id: names.site_id.clone(),
            camera_id: names.camera_id.clone(),
            gateway_id: names.gateway_id.clone(),
            issuer_sha256: format!("{:x}", Sha256::digest(&self.ca_der)),
            enrollment_generation: generation,
            ca_certificate_file: self.directory.join("ca.pem"),
            camera_certificate_file: directory.join("camera.pem"),
            client_certificate_file: directory.join("client.pem"),
            client_key_file: directory.join("client.key"),
            crl_file: directory.join("crl.pem"),
        });
        Ok(config)
    }
    fn resume(&mut self) -> Result<()> {
        if self.generation == 0 {
            return Ok(());
        }
        let config = self.config_for(self.generation)?;
        match crate::check(&config) {
            Ok(()) => {
                let (stop, receiver) = watch::channel(false);
                self.active = Some((stop, tokio::spawn(runner::run(config, receiver))));
            }
            Err(Error::Authentication) => {} // The camera's next renewal supplies a fresh grant.
            Err(error) => return Err(error),
        }
        self.grant_directory = Some(self.directory.join(format!("grant-{}", self.generation)));
        Ok(())
    }
    async fn enroll(
        &mut self,
        csr: &[u8],
        epoch: u64,
        address: std::net::Ipv4Addr,
    ) -> Result<Vec<u8>> {
        if !(address.is_private() || address.is_loopback()) {
            return Err(Error::Authentication);
        }
        let mut request = CertificateSigningRequestParams::from_pem(
            std::str::from_utf8(csr).map_err(|_| Error::Authentication)?,
        )
        .map_err(|_| Error::Authentication)?;
        if request.public_key.algorithm() != &PKCS_ECDSA_P256_SHA256 {
            return Err(Error::Authentication);
        }
        // Ignore all caller-requested names, usages, constraints and validity.
        let names = self.config.bootstrap.as_ref().ok_or(Error::Configuration)?;
        let own_uri = format!("urn:nxrs:camera:{}:{}", names.site_id, names.camera_id);
        let peer_uri = format!(
            "urn:nxrs:ingest:{}:{}:{}",
            names.site_id, names.gateway_id, names.camera_id
        );
        let generation = self
            .generation
            .checked_add(1)
            .filter(|n| *n <= 1_000_000_000)
            .ok_or(Error::Configuration)?;
        request.params = leaf(&own_uri, epoch, generation * 2, false)?;
        request
            .params
            .subject_alt_names
            .push(SanType::IpAddress(address.into()));
        let own = configuration(request.signed_by(&self.issuer))?;
        let client_key = configuration(KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256))?;
        let peer = configuration(
            leaf(&peer_uri, epoch, generation * 2 + 1, true)?.signed_by(&client_key, &self.issuer),
        )?;
        let crl = configuration(
            CertificateRevocationListParams {
                this_update: configuration(OffsetDateTime::from_unix_timestamp(epoch as i64))?,
                next_update: configuration(OffsetDateTime::from_unix_timestamp(
                    epoch as i64 + 3600,
                ))?,
                crl_number: generation.into(),
                issuing_distribution_point: None,
                revoked_certs: vec![],
                key_identifier_method: KeyIdMethod::Sha256,
            }
            .signed_by(&self.issuer),
        )?;
        let enrollment = Enrollment {
            issuer_sha256: Sha256::digest(&self.ca_der).into(),
            own_uri,
            peer_uri,
            own: own.der().as_ref().to_vec(),
            issuer: self.ca_der.clone(),
            approved_peer: peer.der().as_ref().to_vec(),
            revocations: crl.der().as_ref().to_vec(),
        };
        let encoded = configuration(enrollment.encode())?;
        let mut nonce = [0; 16];
        configuration(getrandom::fill(&mut nonce))?;
        let staging = Staging(self.directory.join(format!(
            "pending-{}",
            nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
        )));
        private_dir(&staging.0)?;
        for (name, bytes) in [
            ("camera.ip", address.octets().to_vec()),
            ("camera.pem", own.pem().into_bytes()),
            ("client.pem", peer.pem().into_bytes()),
            ("client.key", client_key.serialize_pem().into_bytes()),
            ("crl.pem", configuration(crl.pem())?.into_bytes()),
        ] {
            let bytes = Zeroizing::new(bytes);
            write_new(&staging.0.join(name), &bytes)?;
        }
        crate::check(&self.config_in(generation, &staging.0)?)?;
        self.retire().await?; // A replacement grant cannot overlap the old ingest/encoder.
        let directory = self.directory.join(format!("grant-{generation}"));
        // An uncommitted orphan from an interrupted prior attempt is never active.
        if directory.exists() {
            fs::remove_dir_all(&directory)?;
        }
        fs::rename(&staging.0, &directory)?;
        let next = self.directory.join("generation.next");
        if next.exists() {
            fs::remove_file(&next)?;
        }
        write_new(&next, format!("{generation}\n").as_bytes())?;
        fs::rename(next, self.directory.join("generation"))?;
        self.generation = generation;
        if let Some(previous) = self.grant_directory.replace(directory) {
            fs::remove_dir_all(previous)?;
        }
        Ok(encoded)
    }
}

/// Read-only authority preflight; an expired or absent media grant must not
/// prevent the endpoint starting to issue its replacement.
pub fn check(config: &Config) -> Result<()> {
    let _ = config.token()?;
    let _ = Authority::load(config.clone())?;
    Ok(())
}

pub async fn run(config: Config, stop: watch::Receiver<bool>) -> Result<()> {
    execute(config, stop, None).await
}
/// Replaces the stopped normal gateway process. Binding the sole bootstrap
/// listener precedes upload; it remains owned through all retirement barriers.
/// After transfer this process continues enrollment/ingest for the new trial.
pub async fn update(config: Config, image: PathBuf, stop: watch::Receiver<bool>) -> Result<()> {
    execute(config, stop, Some(image)).await
}
async fn wait_upload(task: &mut Option<JoinHandle<Result<()>>>) -> Result<()> {
    match task {
        Some(task) => task.await.map_err(|_| Error::Cleanup)?,
        None => std::future::pending().await,
    }
}
/// Retain ownership through cancellation; a concurrent commit must not replay.
async fn cancel_upload(task: &mut Option<JoinHandle<Result<()>>>) -> Result<bool> {
    let Some(owner) = task.as_mut() else {
        return Ok(false);
    };
    owner.abort();
    let result = owner.await;
    *task = None;
    match result {
        Ok(Ok(())) => Ok(true),
        Ok(Err(error @ (Error::Configuration | Error::Cleanup))) => Err(error),
        Err(error) if !error.is_cancelled() => Err(Error::Cleanup),
        _ => Ok(false),
    }
}
fn start_upload(authority: &Authority, image: &Path) -> Result<JoinHandle<Result<()>>> {
    let config = authority.config_for(authority.generation)?;
    crate::check(&config)?;
    let image = image.to_owned();
    Ok(tokio::spawn(async move {
        crate::update::upload(&config, &image).await
    }))
}
async fn execute(
    config: Config,
    mut stop: watch::Receiver<bool>,
    mut image: Option<PathBuf>,
) -> Result<()> {
    let listen = config
        .bootstrap
        .as_ref()
        .ok_or(Error::Configuration)?
        .listen;
    let (mut authority, acceptor) = Authority::load(config)?;
    let listener = TcpListener::bind(listen).await?;
    let upload_deadline = tokio::time::Instant::now() + Duration::from_secs(120);
    let mut upload = if let Some(image) = &image {
        if authority.generation == 0 {
            None
        } else {
            match start_upload(&authority, image) {
                Ok(task) => Some(task),
                Err(Error::Authentication) => None,
                Err(error) => return Err(error),
            }
        }
    } else {
        authority.resume()?;
        None
    };
    let outcome=async {
        loop {
            let socket=tokio::select! {
                biased;
                _=stop.changed()=>return Ok(()),
                _=tokio::time::sleep_until(upload_deadline), if image.is_some()=>return Err(Error::Timeout),
                result=wait_upload(&mut upload)=>{
                    upload=None;
                    match result {
                        Ok(())=>{image=None;println!("Firmware transfer committed; continuing network enrollment and ingest.");},
                        Err(error @ (Error::Configuration|Error::Cleanup))=>return Err(error),
                        Err(_)=>eprintln!("Firmware transfer interrupted; awaiting fresh camera enrollment within the original deadline."),
                    }
                    continue;
                },
                value=listener.accept()=>value?.0,
            };
            let updating=image.is_some();
            let request=async {
                let address=match socket.peer_addr()?.ip() {
                    std::net::IpAddr::V4(ip)=>ip,
                    _=>return Err(Error::Authentication),
                };
                let mut tls=acceptor.accept(socket).await.map_err(|_|Error::Authentication)?;
                let mut header=[0;38];tls.read_exact(&mut header).await?;
                if &header[..4]!=enrollment::MAGIC || header[4..36].ct_eq(&authority.token).unwrap_u8()!=1 { return Err(Error::Authentication); }
                let n=u16::from_be_bytes(header[36..].try_into().unwrap()) as usize;
                if n>enrollment::MAX_CSR { return Err(Error::Protocol); }
                let mut csr=vec![0;n];tls.read_exact(&mut csr).await?;
                let epoch=now()?;
                if epoch<authority.clock { return Err(Error::Authentication); }
                authority.clock=epoch;
                let certificate=if n==0 { vec![] } else {
                    // A reboot changes the volatile key. Join the obsolete
                    // uploader before publishing/retrying against a new grant.
                    if cancel_upload(&mut upload).await? { image=None; }
                    let certificate=authority.enroll(&csr,epoch,address).await?;
                    if let Some(image)=&image { upload=Some(start_upload(&authority,image)?); }
                    else { authority.resume()?; }
                    certificate
                };
                let response_epoch=now()?;
                if response_epoch<authority.clock { return Err(Error::Authentication); }
                authority.clock=response_epoch;
                let mut bytes=Vec::new();enrollment::response(&mut bytes,response_epoch,&certificate)?;
                tls.write_all(&bytes).await?;tls.flush().await?;
                Ok(())
            };
            let result=tokio::select! {
                biased;
                _=stop.changed()=>return Ok(()),
                _=tokio::time::sleep_until(upload_deadline), if updating=>return Err(Error::Timeout),
                value=timeout(Duration::from_secs(5),request)=>value,
            };
            if matches!(result,Ok(Err(Error::Cleanup|Error::Configuration))) { return Err(Error::Cleanup); }
        }
    }.await;
    if let Some(task) = upload {
        task.abort();
        let _ = task.await;
    }
    let retired = authority.retire().await;
    drop(listener);
    outcome?;
    retired
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (PathBuf, Config) {
        let root = std::env::temp_dir().join(format!(
            "nxrs-enroll-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        private_dir(&root).unwrap();
        write_new(
            &root.join("publisher.token"),
            b"1111111111111111111111111111111111111111111111111111111111111111",
        )
        .unwrap();
        let config:Config=serde_json::from_value(serde_json::json!({
            "camera":"127.0.0.1:9","allow_insecure_camera_http":false,"rtsp":"127.0.0.1:8554","stream_path":"camera","publisher_user":"publisher",
            "publisher_token_file":root.join("publisher.token"),"ffmpeg":"/missing/ffmpeg","publisher":"/missing/publisher","width":320,"height":240,"frame_timeout_ms":3000,
            "bootstrap":{"listen":"127.0.0.1:8444","directory":root.join("authority"),"site_id":"s","camera_id":"c","gateway_id":"g"}
        })).unwrap();
        (root, config)
    }
    #[tokio::test]
    async fn verified_csr_permissions_are_replaced_and_ingest_retirement_survives_cancellation() {
        let (root, config) = fixture();
        initialize(&config).unwrap();
        assert!(initialize(&config).is_err());
        let packet = config::private_read(
            &root.join("authority/commission.bin"),
            Commission::MAX_BYTES as u64,
        )
        .unwrap();
        let commission = Commission::decode(&packet).unwrap();
        assert_eq!(commission.camera, "c");
        // Process interruption before pointer publication must not admit an
        // incomplete next grant. The next enrollment replaces that orphan.
        private_dir(&root.join("authority/grant-1")).unwrap();
        write_new(&root.join("authority/grant-1/orphan"), b"incomplete").unwrap();
        write_new(&root.join("authority/generation.next"), b"1\n").unwrap();
        check(&config).unwrap();
        let (mut authority, _) = Authority::load(config.clone()).unwrap();
        assert_eq!(authority.generation, 0);
        assert!(authority
            .enroll(
                b"invalid CSR",
                now().unwrap(),
                std::net::Ipv4Addr::LOCALHOST
            )
            .await
            .is_err());
        assert_eq!(authority.generation, 0);
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).unwrap();
        let mut malicious = CertificateParams::new(vec!["evil.invalid".into()]).unwrap();
        malicious.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        malicious.key_usages = vec![KeyUsagePurpose::KeyCertSign];
        let request = malicious.serialize_request(&key).unwrap().pem().unwrap();
        let encoded = authority
            .enroll(
                request.as_bytes(),
                now().unwrap(),
                "127.0.0.2".parse().unwrap(),
            )
            .await
            .unwrap();
        let grant = Enrollment::decode(&encoded).unwrap();
        assert_eq!(grant.own_uri, commission.own_uri());
        let (_, certificate) = x509_parser::parse_x509_certificate(&grant.own).unwrap();
        let basic = certificate.basic_constraints().unwrap().unwrap();
        assert!(basic.critical);
        assert!(!basic.value.ca);
        let eku = certificate.extended_key_usage().unwrap().unwrap();
        assert!(eku.value.server_auth);
        assert!(!eku.value.client_auth);
        crate::check(&authority.config_for(1).unwrap()).unwrap();
        assert_eq!(
            authority.config_for(1).unwrap().camera.ip().octets(),
            [127, 0, 0, 2]
        );
        assert!(!root.join("authority/grant-1/orphan").exists());
        authority.retire().await.unwrap();
        let (release, wait) = tokio::sync::oneshot::channel();
        let (stop, _) = watch::channel(false);
        authority.active = Some((
            stop,
            tokio::spawn(async {
                let _ = wait.await;
                Ok(())
            }),
        ));
        assert!(timeout(Duration::from_millis(5), authority.retire())
            .await
            .is_err());
        assert!(authority.active.is_some());
        release.send(()).unwrap();
        authority.retire().await.unwrap();
        assert!(authority.active.is_none());
        let (restarted, _) = Authority::load(config).unwrap();
        assert_eq!(restarted.generation, 1);
        drop(restarted);
        for (outcome, expected) in [
            (Ok(()), Ok(true)),
            (Err(Error::Io), Ok(false)),
            (Err(Error::Configuration), Err(Error::Configuration)),
        ] {
            let mut upload = Some(tokio::spawn(async move { outcome }));
            tokio::task::yield_now().await; // Complete before the cancellation races it.
            assert_eq!(cancel_upload(&mut upload).await, expected);
            assert!(upload.is_none());
        }
        fs::remove_dir_all(root).unwrap();
    }
}

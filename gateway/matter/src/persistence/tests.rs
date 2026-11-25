use super::*;

#[test]
fn bounded_format_rejects_corruption_noncanonical_and_oversized_state() {
    let mut snapshot = Snapshot::default();
    snapshot
        .entries
        .insert(7, Zeroizing::new(b"test data".to_vec()));
    let encoded = snapshot.encode().unwrap();
    assert_eq!(
        Snapshot::decode(&encoded).unwrap().entries,
        snapshot.entries
    );
    for end in 0..encoded.len() {
        assert!(Snapshot::decode(&encoded[..end]).is_err());
    }
    let mut bad = encoded.clone();
    bad[24] ^= 1;
    assert!(Snapshot::decode(&bad).is_err());
    // Re-sign deliberately malformed metadata so these cases reach the parser.
    for (offset, value) in [
        (16, 513u16.to_le_bytes().to_vec()),
        (20, (MAX_VALUE_BYTES as u32 + 1).to_le_bytes().to_vec()),
    ] {
        let mut bad = encoded[..encoded.len() - 32].to_vec();
        bad[offset..offset + value.len()].copy_from_slice(&value);
        bad.extend_from_slice(&Sha256::digest(&bad));
        assert!(Snapshot::decode(&bad).is_err());
    }
    snapshot
        .entries
        .insert(8, Zeroizing::new(vec![0; MAX_VALUE_BYTES + 1]));
    assert!(matches!(snapshot.encode(), Err(Error::Capacity)));
    assert!(Snapshot::decode(&vec![0; MAX_SNAPSHOT_BYTES + 1]).is_err());
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
mod files {
    use super::*;
    use std::os::unix::fs::{symlink, DirBuilderExt, PermissionsExt};
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT_ID: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "nxrs-matter-state-{}-{}",
                std::process::id(),
                NEXT_ID.fetch_add(1, Ordering::Relaxed)
            ));
            fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> PathBuf {
            self.0.join("node")
        }
        fn initialize(&self) -> Store {
            Store::initialize(&self.path()).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn value(store: &Store, key: u16) -> Option<Vec<u8>> {
        store.load(key, &mut [0; 128]).unwrap().map(Vec::from)
    }

    #[test]
    fn tls_roots_rotate_and_retire_with_their_fabric() {
        use crate::tls::{Error, RootCertificates, MAX_CERTIFICATE_BYTES, MAX_ROOT_CERTIFICATES};
        use rcgen::{BasicConstraints, CertificateParams, DnType, IsCa, Issuer, KeyPair};

        let fixture = Fixture::new();
        let mut store = fixture.initialize();
        let now = Some(1_788_000_000);
        let ca = |name| {
            let mut params = CertificateParams::default();
            params.distinguished_name.push(DnType::CommonName, name);
            params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
            let key = KeyPair::generate().unwrap();
            let root = params.self_signed(&key).unwrap();
            (root, Issuer::new(params, key))
        };
        let (first, issuer) = ca("first.example");
        let (second, _) = ca("second.example");
        let leaf_key = KeyPair::generate().unwrap();
        let leaf = CertificateParams::new(vec!["camera.example".into()])
            .unwrap()
            .signed_by(&leaf_key, &issuer)
            .unwrap();
        let mut roots = RootCertificates::open(&mut store, 1).unwrap();
        assert_eq!(
            roots.provision(None, first.der(), None),
            Err(Error::InvalidTime)
        );
        assert_eq!(
            roots.provision(None, &vec![0; MAX_CERTIFICATE_BYTES + 1], now),
            Err(Error::Constraint)
        );
        assert_eq!(
            roots.provision(None, leaf.der(), now),
            Err(Error::InvalidCertificate)
        );
        let mut broken = first.der().to_vec();
        *broken.last_mut().unwrap() ^= 1;
        assert_eq!(
            roots.provision(None, &broken, now),
            Err(Error::InvalidCertificate)
        );
        let id = roots.provision(None, first.der(), now).unwrap();
        assert_eq!(
            roots.provision(None, first.der(), now),
            Err(Error::AlreadyExists)
        );
        assert_eq!(
            roots.provision(Some(19), second.der(), now),
            Err(Error::NotFound)
        );
        for _ in 1..MAX_ROOT_CERTIFICATES {
            let (extra, _) = ca("extra.example");
            roots.provision(None, extra.der(), now).unwrap();
        }
        assert_eq!(
            roots.provision(None, second.der(), now),
            Err(Error::Capacity)
        );
        let fingerprint = Sha256::digest(first.der());
        assert_eq!(roots.lookup(&fingerprint), Ok(id));
        assert_eq!(
            roots.trust_anchor(id, Some(0)),
            Err(Error::InvalidCertificate)
        );

        let mut other = RootCertificates::open(&mut store, 2).unwrap();
        assert_eq!(other.lookup(&fingerprint), Err(Error::NotFound));
        assert_eq!(other.remove(id), Err(Error::NotFound));
        let other_id = other.provision(None, first.der(), now).unwrap();
        assert_ne!(id, other_id);
        drop(store);

        let mut store = Store::open(&fixture.path()).unwrap();
        let mut roots = RootCertificates::open(&mut store, 1).unwrap();
        assert_eq!(roots.lookup(&fingerprint), Ok(id));
        assert_eq!(
            tls_handshake(roots.trust_anchor(id, now).unwrap(), &leaf, &leaf_key, None),
            Ok(()),
        );
        assert_eq!(roots.provision(Some(id), second.der(), now), Ok(id));
        assert_eq!(roots.lookup(&fingerprint), Err(Error::NotFound));
        assert_eq!(
            tls_handshake(roots.trust_anchor(id, now).unwrap(), &leaf, &leaf_key, None),
            Err(rustls::Error::InvalidCertificate(
                rustls::CertificateError::UnknownIssuer
            )),
        );
        let fabric_record = 0x0050;
        store.store(fabric_record, b"test fabric").unwrap();
        store
            .remove_with(fabric_record, |key| {
                Some(key) == RootCertificates::record_key(1)
            })
            .unwrap();
        drop(store);

        let mut store = Store::open(&fixture.path()).unwrap();
        assert_eq!(value(&store, fabric_record), None);
        assert!(RootCertificates::open(&mut store, 1)
            .unwrap()
            .list()
            .unwrap()
            .is_empty());
        let other = RootCertificates::open(&mut store, 2).unwrap();
        assert_eq!(other.lookup(&fingerprint), Ok(other_id));
        assert_eq!(
            tls_handshake(
                other.trust_anchor(other_id, now).unwrap(),
                &leaf,
                &leaf_key,
                None
            ),
            Ok(())
        );
    }

    #[test]
    fn tls_client_key_survives_restart_and_authenticates_after_renewal() {
        use crate::tls::{ClientCertificates, Error};
        use rcgen::{
            BasicConstraints, CertificateParams, CertificateSigningRequestParams, DnType,
            ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
        };
        use ring::signature::{UnparsedPublicKey, ECDSA_P256_SHA256_FIXED};
        use x509_parser::{certification_request::X509CertificationRequest, prelude::FromDer};

        let fixture = Fixture::new();
        let mut store = fixture.initialize();
        let nonce = [7; 32];
        let now = Some(1_788_000_000);
        let mut clients = ClientCertificates::open(&mut store, 1).unwrap();
        assert!(matches!(
            clients.request_csr(None, &nonce[..31]),
            Err(Error::Constraint)
        ));
        let first = clients.request_csr(None, &nonce).unwrap();
        let vacant = clients.request_csr(None, &nonce).unwrap();
        assert!(matches!(
            clients.request_csr(None, &nonce),
            Err(Error::Capacity)
        ));
        clients.remove(vacant.id).unwrap();
        assert!(matches!(
            clients.identity(first.id, now),
            Err(Error::CertificatePending)
        ));
        assert!(clients.list().unwrap()[0].leaf.is_none());
        let mut other = ClientCertificates::open(&mut store, 2).unwrap();
        assert!(matches!(
            other.request_csr(Some(first.id), &nonce),
            Err(Error::NotFound)
        ));
        let other_id = other.request_csr(None, &nonce).unwrap().id;
        assert_ne!(first.id, other_id);
        drop(store);

        let mut store = Store::open(&fixture.path()).unwrap();
        let mut clients = ClientCertificates::open(&mut store, 1).unwrap();
        let renewal_nonce = [8; 32];
        let renewal = clients.request_csr(Some(first.id), &renewal_nonce).unwrap();
        let verify = |request: &crate::tls::SigningRequest, nonce: &[u8]| {
            let (rest, csr) = X509CertificationRequest::from_der(&request.der).unwrap();
            assert!(rest.is_empty());
            csr.verify_signature().unwrap();
            let public = csr
                .certification_request_info
                .subject_pki
                .subject_public_key
                .data;
            let verifier = UnparsedPublicKey::new(&ECDSA_P256_SHA256_FIXED, public.as_ref());
            verifier.verify(nonce, &request.nonce_signature).unwrap();
            assert!(verifier.verify(&[9; 32], &request.nonce_signature).is_err());
            public.into_owned()
        };
        assert_eq!(verify(&first, &nonce), verify(&renewal, &renewal_nonce));

        let mut ca = CertificateParams::default();
        ca.distinguished_name.push(DnType::CommonName, "test root");
        ca.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca_key = KeyPair::generate().unwrap();
        let root = ca.self_signed(&ca_key).unwrap();
        let ca = Issuer::new(ca, ca_key);
        let mut intermediate = CertificateParams::default();
        intermediate
            .distinguished_name
            .push(DnType::CommonName, "test intermediate");
        intermediate.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
        let intermediate_key = KeyPair::generate().unwrap();
        let intermediate_cert = intermediate.signed_by(&intermediate_key, &ca).unwrap();
        let intermediate = Issuer::new(intermediate, intermediate_key);
        let sign = |req: &crate::tls::SigningRequest, serial: u64| {
            let mut params =
                CertificateSigningRequestParams::from_der(&req.der.as_slice().into()).unwrap();
            params.params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
            params.params.serial_number = Some(serial.into());
            params.signed_by(&intermediate).unwrap()
        };
        let leaf = sign(&first, 1);
        let renewed = sign(&renewal, 2);
        let chain = &[intermediate_cert.der().as_ref()];
        let server_key = KeyPair::generate().unwrap();
        let server = CertificateParams::new(vec!["camera.example".into()])
            .unwrap()
            .signed_by(&server_key, &ca)
            .unwrap();
        assert_eq!(
            clients.provision(first.id, server.der(), chain, now),
            Err(Error::InvalidCertificate)
        );
        assert_eq!(
            clients.provision(first.id, leaf.der(), chain, None),
            Err(Error::InvalidTime)
        );
        clients.provision(first.id, leaf.der(), chain, now).unwrap();
        let old_fingerprint = Sha256::digest(leaf.der());
        assert_eq!(clients.lookup(&old_fingerprint), Ok(first.id));
        assert_eq!(
            clients.provision(first.id, leaf.der(), chain, now),
            Err(Error::AlreadyExists)
        );
        assert_eq!(
            clients.provision(first.id, renewed.der(), &[b"invalid"], now),
            Err(Error::InvalidCertificate)
        );
        tls_handshake(
            root.der().to_vec(),
            &server,
            &server_key,
            Some(clients.identity(first.id, now).unwrap()),
        )
        .unwrap();
        clients
            .provision(first.id, renewed.der(), chain, now)
            .unwrap();
        assert_eq!(clients.lookup(&old_fingerprint), Err(Error::NotFound));
        drop(store);

        let mut store = Store::open(&fixture.path()).unwrap();
        let clients = ClientCertificates::open(&mut store, 1).unwrap();
        assert!(matches!(
            clients.identity(first.id, Some(0)),
            Err(Error::InvalidCertificate)
        ));
        let identity = clients.identity(first.id, now).unwrap();
        assert_eq!(identity.end_entity_cert().unwrap(), renewed.der());
        tls_handshake(root.der().to_vec(), &server, &server_key, Some(identity)).unwrap();
        let slots = ClientCertificates::record_keys(1).unwrap();
        store.store(0x0050, b"test fabric").unwrap();
        store
            .remove_with(0x0050, |key| slots.contains(&key))
            .unwrap();
        drop(store);
        let mut store = Store::open(&fixture.path()).unwrap();
        assert!(ClientCertificates::open(&mut store, 1)
            .unwrap()
            .list()
            .unwrap()
            .is_empty());
        let other = ClientCertificates::open(&mut store, 2).unwrap();
        assert_eq!(other.list().unwrap()[0].id, other_id);
    }

    fn tls_handshake(
        root: Vec<u8>,
        leaf: &rcgen::Certificate,
        key: &rcgen::KeyPair,
        identity: Option<rustls::sign::CertifiedKey>,
    ) -> Result<(), rustls::Error> {
        use rustls::{
            pki_types::PrivatePkcs8KeyDer, server::WebPkiClientVerifier, sign::SingleCertAndKey,
            ClientConfig, ClientConnection, RootCertStore, ServerConfig, ServerConnection,
        };
        use std::sync::Arc;

        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut roots = RootCertStore::empty();
        roots.add(root.into()).unwrap();
        let client = ClientConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_root_certificates(roots.clone());
        let server = ServerConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .unwrap();
        let expected_client = identity.as_ref().map(|id| id.cert[0].clone());
        let (client, server) = if let Some(identity) = identity {
            let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider)
                .build()
                .unwrap();
            (
                client.with_client_cert_resolver(Arc::new(SingleCertAndKey::from(identity))),
                server.with_client_cert_verifier(verifier),
            )
        } else {
            (client.with_no_client_auth(), server.with_no_client_auth())
        };
        let server = server
            .with_single_cert(
                vec![leaf.der().clone()],
                PrivatePkcs8KeyDer::from(key.serialize_der()).into(),
            )
            .unwrap();
        let mut client =
            ClientConnection::new(Arc::new(client), "camera.example".try_into().unwrap()).unwrap();
        let mut server = ServerConnection::new(Arc::new(server)).unwrap();
        for _ in 0..8 {
            let mut flight = Vec::new();
            client.write_tls(&mut flight).unwrap();
            server.read_tls(&mut flight.as_slice()).unwrap();
            server.process_new_packets()?;
            flight.clear();
            server.write_tls(&mut flight).unwrap();
            client.read_tls(&mut flight.as_slice()).unwrap();
            client.process_new_packets()?;
            if !client.is_handshaking() && !server.is_handshaking() {
                assert_eq!(
                    server.peer_certificates().and_then(|certs| certs.first()),
                    expected_client.as_ref()
                );
                return Ok(());
            }
        }
        panic!("local TLS handshake did not finish");
    }

    #[test]
    fn interrupted_onboarding_stays_uncommissionable_on_restart() {
        use crate::commissioning::{Error as OnboardingError, FactoryData};
        let fixture = Fixture::new();
        drop(fixture.initialize()); // Process exited before recording its factory identity.
        let mut state = Store::open(&fixture.path()).unwrap();
        let factory = FactoryData {
            discriminator: 1,
            iterations: 10000,
            salt: vec![1; 16],
            verifier: [1; 97],
        };
        assert_eq!(
            factory.bind(&mut state, 0x1003, false),
            Err(OnboardingError::IdentityMismatch)
        );
        assert!(state.load(0x1003, &mut [0; 32]).unwrap().is_none());
    }

    #[test]
    fn explicit_initialization_single_writer_and_durable_mutations() {
        let fixture = Fixture::new();
        assert!(Store::open(&fixture.path()).is_err());
        let mut store = fixture.initialize();
        assert!(Store::initialize(&fixture.path()).is_err());
        assert!(matches!(
            Store::open(&fixture.path()),
            Err(Error::AlreadyOwned)
        ));
        store.store(1, b"first fabric test data").unwrap();
        store.store(2, b"second fabric test data").unwrap();
        assert_eq!(store.load(1, &mut []), Err(Error::BufferTooSmall));
        drop(store);
        let mut store = Store::open(&fixture.path()).unwrap();
        assert_eq!(value(&store, 2), Some(b"second fabric test data".to_vec()));
        store.remove(1).unwrap();
        store.remove(1).unwrap();
        drop(store);
        let store = Store::open(&fixture.path()).unwrap();
        assert_eq!(value(&store, 1), None);
        assert_eq!(value(&store, 2), Some(b"second fabric test data".to_vec()));
        assert_eq!(
            fs::metadata(fixture.path().join(STATE))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    #[test]
    fn missing_corrupt_or_linked_state_never_becomes_empty() {
        let fixture = Fixture::new();
        let mut store = fixture.initialize();
        store.store(1, b"test identity").unwrap();
        drop(store);
        let path = fixture.path().join(STATE);
        let valid = fs::read(&path).unwrap();
        for bytes in [&[][..], &valid[..valid.len() - 1]] {
            fs::write(&path, bytes).unwrap();
            assert!(matches!(Store::open(&fixture.path()), Err(Error::Corrupt)));
        }
        fs::remove_file(&path).unwrap();
        assert!(Store::open(&fixture.path()).is_err());
        let other = fixture.0.join("other");
        fs::write(&other, &valid).unwrap();
        fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&other, &path).unwrap();
        assert!(Store::open(&fixture.path()).is_err());
        fs::remove_file(&path).unwrap();
        fs::hard_link(&other, &path).unwrap();
        assert!(Store::open(&fixture.path()).is_err());
        fs::remove_file(&path).unwrap();
        fs::rename(&other, &path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(Store::open(&fixture.path()).is_err());
    }

    #[test]
    fn abandoned_staging_is_never_promoted() {
        let fixture = Fixture::new();
        let mut store = fixture.initialize();
        store.store(1, b"committed").unwrap();
        let mut next = store.snapshot.clone();
        next.entries
            .insert(1, Zeroizing::new(b"uncommitted".to_vec()));
        let encoded = next.encode().unwrap();
        drop(store);
        for bytes in [&encoded[..5], &encoded[..]] {
            let mut staged = platform::open_file(&fixture.path().join(NEXT), true, true).unwrap();
            staged.write_all(bytes).unwrap();
            staged.sync_all().unwrap();
            drop(staged);
            let store = Store::open(&fixture.path()).unwrap();
            assert_eq!(value(&store, 1), Some(b"committed".to_vec()));
            assert!(!fixture.path().join(NEXT).exists());
        }
        // Losing the authoritative snapshot does not authorize orphan recovery.
        fs::rename(fixture.path().join(STATE), fixture.path().join(NEXT)).unwrap();
        assert!(Store::open(&fixture.path()).is_err());
        assert!(Store::initialize(&fixture.path()).is_err());
    }

    #[test]
    fn io_failure_poison_is_sticky_and_preserves_committed_state() {
        let fixture = Fixture::new();
        let mut store = fixture.initialize();
        store.store(1, b"committed").unwrap();
        fs::create_dir(fixture.path().join(NEXT)).unwrap();
        assert_eq!(store.store(1, b"not committed"), Err(Error::Failed));
        fs::remove_dir(fixture.path().join(NEXT)).unwrap();
        assert_eq!(store.load(1, &mut [0; 128]), Err(Error::Failed));
        assert_eq!(store.remove(1), Err(Error::Failed));
        drop(store);
        assert_eq!(
            value(&Store::open(&fixture.path()).unwrap(), 1),
            Some(b"committed".to_vec())
        );
    }

    #[test]
    fn replaced_lock_or_snapshot_stops_the_existing_writer() {
        for replace_lock in [false, true] {
            let fixture = Fixture::new();
            let mut store = fixture.initialize();
            store.store(1, b"committed").unwrap();
            if replace_lock {
                fs::rename(fixture.path().join(LOCK), fixture.path().join("old-lock")).unwrap();
                platform::open_file(&fixture.path().join(LOCK), true, true).unwrap();
            } else {
                fs::write(
                    fixture.path().join(STATE),
                    Snapshot::default().encode().unwrap(),
                )
                .unwrap();
            }
            assert_eq!(store.store(2, b"new"), Err(Error::Changed));
            assert_eq!(store.load(1, &mut [0; 128]), Err(Error::Failed));
        }
    }
}

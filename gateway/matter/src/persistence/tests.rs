use super::*;

#[test]
fn bounded_format_rejects_corruption_noncanonical_and_oversized_state() {
    let mut snapshot = Snapshot::default();
    snapshot.entries.insert(7, b"test data".to_vec());
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
    snapshot.entries.insert(8, vec![0; MAX_VALUE_BYTES + 1]);
    assert_eq!(snapshot.encode(), Err(Error::Capacity));
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
        next.entries.insert(1, b"uncommitted".to_vec());
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

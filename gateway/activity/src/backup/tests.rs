//! Executed native-backend controls. These do not claim an encrypted-volume run on macOS.
use super::*;
use std::os::unix::fs::PermissionsExt;
struct Fixture {
    root: PathBuf,
    config: Config,
    key: Zeroizing<Vec<u8>>,
}
impl Fixture {
    fn new(binary: PathBuf) -> Self {
        let mut entropy = [0; 32];
        getrandom::fill(&mut entropy).unwrap();
        let key = Zeroizing::new(
            entropy
                .iter()
                .map(|v| format!("{v:02x}"))
                .collect::<String>()
                .into_bytes(),
        );
        let mut nonce = [0; 16];
        getrandom::fill(&mut nonce).unwrap();
        let id = nonce.iter().map(|v| format!("{v:02x}")).collect::<String>();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("nxrs-restic-test-{id}"));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        for path in [
            root.join("volume"),
            root.join("volume/maintenance"),
            root.join("volume/maintenance/temporary"),
            root.join("repo"),
        ] {
            fs::DirBuilder::new().mode(0o700).create(path).unwrap();
        }
        let storage = Settings {
            directory: root.join("volume/activity"),
            camera_ids: vec!["cam".into()],
            retention_seconds: 86400,
            max_bytes: 64 * 1024 * 1024,
            max_events: 100,
            protection: Some(Volume {
                layout_version: 2,
                mount: root.join("volume"),
                luks_uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
            }),
        };
        let digest = Sha256::digest(fs::read(&binary).unwrap());
        let config = Config {
            storage,
            repository: root.join("repo"),
            repository_id: None,
            restic: binary,
            restic_sha256: format!("{digest:x}"),
            timeout_seconds: 60,
        };
        Self { root, config, key }
    }
    fn backend(&self, stop: watch::Receiver<bool>, seconds: u64) -> process::Restic<'_> {
        process::Restic {
            config: &self.config,
            key: &self.key,
            deadline: Instant::now() + Duration::from_secs(seconds),
            stop,
        }
    }
    fn source(&self) -> Settings {
        let mut settings = self.config.storage.clone();
        settings.protection = None;
        settings
    }
    fn witness() -> PathBuf {
        PathBuf::from(
            std::env::var_os("NXRS_TEST_RESTIC")
                .expect("set NXRS_TEST_RESTIC to the verified artifact"),
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn run_id(bytes: &[u8]) -> String {
    bytes
        .split(|v| *v == b'\n')
        .filter_map(|line| serde_json::from_slice::<serde_json::Value>(line).ok())
        .find(|value| value["message_type"] == "summary")
        .unwrap()["snapshot_id"]
        .as_str()
        .unwrap()
        .into()
}
fn files(root: &Path) -> Vec<PathBuf> {
    let mut result = Vec::new();
    for entry in fs::read_dir(root).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_dir() {
            result.extend(files(&entry.path()));
        } else {
            result.push(entry.path());
        }
    }
    result
}
#[tokio::test]
#[ignore = "requires exact checksum-verified restic binary; exercised explicitly in host qualification"]
async fn actual_restic_authenticates_encrypts_verifies_and_restores_a_frozen_store() {
    let fixture = Fixture::new(Fixture::witness());
    fixture.config.verify_binary().unwrap();
    let (stop, rx) = watch::channel(false);
    let backend = fixture.backend(rx, 60);
    let settings = fixture.source();
    let store = Store::open(settings.clone()).unwrap();
    let event = store
        .publish(
            "cam",
            crate::now_ms(),
            crate::now_ms() + 1000,
            b"NXRS-SYNTHETIC-VIDEO-NO-PRIVATE-FOOTAGE",
            b"NXRS-SYNTHETIC-THUMB",
            false,
        )
        .unwrap();
    let _frozen = Store::frozen(&settings).unwrap();
    assert_eq!(store.prune(crate::now_ms()), Err(store::Error::Busy));
    backend
        .command(&["init", "--repository-version", "2"])
        .await
        .unwrap();
    let repository = backend.repository(None).await.unwrap();
    assert!(hex(&repository, 64));
    assert_eq!(
        backend.repository(Some(&"f".repeat(64))).await.err(),
        Some(Error::Repository)
    );
    let output = backend
        .command(&[
            "backup",
            "--force",
            "--one-file-system",
            "--host",
            "nxrs-activity",
            "activity",
        ])
        .await
        .unwrap();
    let snapshot = run_id(&output);
    assert!(hex(&snapshot, 64));
    let listing = backend
        .bounded(&["ls", &format!("{snapshot}:/activity")], 16 * 1024 * 1024)
        .await
        .unwrap();
    manifest::validate(&fixture.config, &listing).unwrap();
    let baseline: Vec<serde_json::Value> = listing
        .split(|v| *v == b'\n')
        .filter(|v| !v.is_empty())
        .map(|line| serde_json::from_slice(line).unwrap())
        .collect();
    for (field, value) in [
        ("path", serde_json::json!("/../../private/key")),
        ("type", serde_json::json!("symlink")),
        ("mode", serde_json::json!(0o644)),
        ("size", serde_json::json!(u64::MAX)),
    ] {
        let mut changed = baseline.clone();
        let node = changed
            .iter_mut()
            .find(|value| value["type"] == "file")
            .unwrap();
        node[field] = value;
        let bytes = changed
            .iter()
            .map(|value| serde_json::to_string(value).unwrap())
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(
            manifest::validate(&fixture.config, bytes.as_bytes()),
            Err(Error::Recovery)
        );
    }
    let mut changed = baseline.clone();
    changed.push(baseline.last().unwrap().clone());
    let bytes = changed
        .iter()
        .map(|value| serde_json::to_string(value).unwrap())
        .collect::<Vec<_>>()
        .join("\n");
    assert_eq!(
        manifest::validate(&fixture.config, bytes.as_bytes()),
        Err(Error::Recovery)
    );

    backend.check().await.unwrap();
    for path in files(&fixture.config.repository) {
        let data = fs::read(path).unwrap();
        assert!(!data
            .windows(fixture.key.len())
            .any(|window| window == fixture.key.as_slice()));
        let plaintext = b"NXRS-SYNTHETIC-VIDEO-NO-PRIVATE-FOOTAGE";
        assert!(!data
            .windows(plaintext.len())
            .any(|window| window == plaintext));
    }
    let wrong = Zeroizing::new(vec![b'0'; 64]);
    let wrong_backend = process::Restic {
        config: &fixture.config,
        key: &wrong,
        deadline: Instant::now() + Duration::from_secs(20),
        stop: stop.subscribe(),
    };
    assert_eq!(
        wrong_backend.repository(None).await.err(),
        Some(Error::Authentication)
    );
    let target = fixture.config.volume().mount.join("stage");
    fs::DirBuilder::new().mode(0o700).create(&target).unwrap();
    backend
        .command(&[
            "restore",
            &format!("{snapshot}:/activity"),
            "--target",
            target.to_str().unwrap(),
            "--verify",
            "--overwrite",
            "never",
            "--exclude-xattr",
            "*",
        ])
        .await
        .unwrap();
    Store::inspect_snapshot(&settings, &target).unwrap();
    let restored = target.join("events").join(&event.event_id).join("clip.mp4");
    assert_eq!(
        fs::read(restored).unwrap(),
        b"NXRS-SYNTHETIC-VIDEO-NO-PRIVATE-FOOTAGE"
    );
    let pack = files(&fixture.config.repository.join("data"))
        .pop()
        .unwrap();
    let mut data = fs::read(&pack).unwrap();
    let last = data.len() - 1;
    data[last] ^= 1;
    // Restic objects are immutable/read-only; only this owned fixture simulates
    // storage corruption after relaxing the test file's write permission.
    fs::set_permissions(&pack, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&pack, data).unwrap();
    assert_eq!(backend.check().await.err(), Some(Error::Native));
}
#[tokio::test]
async fn actual_child_cancel_and_deadline_retire_before_unlocking_the_volume() {
    // The witness receives the same private stdin and exact native supervisor,
    // but never handles real media or calls the restic cryptography backend.
    let fixture = Fixture::new(PathBuf::from("/bin/sleep"));
    let script = fixture.root.join("witness");
    let marker = fixture.root.join("pid");
    fs::write(
        &script,
        format!(
            "#!/bin/sh\nprintf '%s' $$ > '{}'\nexec /bin/sleep 30\n",
            marker.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
    let mut config = fixture.config.clone();
    config.restic = script;
    let volume = config.volume().clone();
    let lease = volume.lease(Access::Maintenance).unwrap();
    let (stop, rx) = watch::channel(false);
    let backend = process::Restic {
        config: &config,
        key: &fixture.key,
        deadline: Instant::now() + Duration::from_secs(5),
        stop: rx,
    };
    let cancel = async {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !fs::read_to_string(&marker)
            .ok()
            .is_some_and(|value| value.trim().parse::<u32>().is_ok_and(|pid| pid > 1))
        {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(
            volume.lease(Access::Recording).err(),
            Some(store::Error::Busy)
        );
        stop.send(true).unwrap();
    };
    let (result, ()) = tokio::join!(backend.command(&["check"]), cancel);
    assert_eq!(result.err(), Some(Error::Cancelled));
    let pid = fs::read_to_string(&marker).unwrap();
    assert!(pid.trim().parse::<u32>().is_ok_and(|value| value > 1));
    assert!(!std::process::Command::new("/bin/kill")
        .args(["-0", pid.trim()])
        .output()
        .unwrap()
        .status
        .success());
    fs::remove_file(&marker).unwrap();
    let (_running, rx) = watch::channel(false);
    let backend = process::Restic {
        config: &config,
        key: &fixture.key,
        deadline: Instant::now() + Duration::from_millis(200),
        stop: rx,
    };
    assert_eq!(
        backend.command(&["check"]).await.err(),
        Some(Error::Timeout)
    );
    let pid = fs::read_to_string(marker).unwrap();
    assert!(!std::process::Command::new("/bin/kill")
        .args(["-0", pid.trim()])
        .output()
        .unwrap()
        .status
        .success());
    drop(lease);
    volume.lease(Access::Recording).unwrap();
}

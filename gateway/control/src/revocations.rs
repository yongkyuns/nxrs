//! Single-writer, fail-closed revocation snapshots for a trusted local filesystem.
use crate::config::identifier;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, fs::{self, File, OpenOptions}, io::{Read, Write}, path::{Path, PathBuf}};
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

const MAX_BYTES: u64 = 1_048_576;
const MAX_REVOKED: usize = 4096;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot { version: u32, revoked: Vec<String> }

pub struct Store {
    path: PathBuf,
    _lock: File,
    pub revoked: BTreeSet<String>,
}

pub fn private_read(path: &Path, limit: u64) -> Result<Vec<u8>, &'static str> {
    if !cfg!(unix) { return Err("private files require Unix descriptor checks"); }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    read_opened(options.open(path).map_err(|_| "private file unavailable")?, limit)
}

fn check_file(file: &File, limit: u64) -> Result<(), &'static str> {
    let metadata = file.metadata().map_err(|_| "private file metadata unavailable")?;
    if !metadata.is_file() { return Err("private file must be regular"); }
    #[cfg(unix)]
    if metadata.mode() & 0o077 != 0 || metadata.nlink() != 1 {
        return Err("private file permissions or links invalid");
    }
    if metadata.len() > limit { return Err("private file exceeds bound"); }
    Ok(())
}

fn read_opened(file: File, limit: u64) -> Result<Vec<u8>, &'static str> {
    let bound = limit.checked_add(1).ok_or("invalid private file bound")?;
    check_file(&file, limit)?;
    let mut bytes = Vec::new();
    (&file).take(bound)
        .read_to_end(&mut bytes).map_err(|_| "private file read failed")?;
    check_file(&file, limit)?;
    if bytes.len() as u64 > limit { return Err("private file exceeds bound"); }
    Ok(bytes)
}

impl Store {
    /// The seed must be explicitly provisioned. Missing/corrupt state never becomes an empty list.
    pub fn open(path: &Path) -> Result<Self, &'static str> {
        if !cfg!(unix) || !path.is_absolute() { return Err("revocation store requires an absolute Unix path"); }
        let parent = path.parent().ok_or("invalid state path")?;
        let metadata = fs::symlink_metadata(parent).map_err(|_| "state directory unavailable")?;
        if !metadata.is_dir() { return Err("state directory must not be a symlink"); }
        #[cfg(unix)]
        if metadata.mode() & 0o077 != 0 { return Err("state directory must be private"); }
        let lock_path = parent.join(".nxrs-revocations.lock");
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        let lock = options.open(lock_path).map_err(|_| "state lock unavailable")?;
        check_file(&lock, 0)?;
        lock.try_lock().map_err(|_| "revocation store already owned or lock unsupported")?;
        check_file(&lock, 0)?;
        let bytes = private_read(path, MAX_BYTES)?;
        let snapshot: Snapshot = serde_json::from_slice(&bytes).map_err(|_| "invalid revocation state")?;
        let revoked: BTreeSet<_> = snapshot.revoked.iter().cloned().collect();
        if snapshot.version != 1 || revoked.len() > MAX_REVOKED
            || revoked.len() != snapshot.revoked.len() || revoked.iter().any(|id| !identifier(id)) {
            return Err("invalid revocation state");
        }
        Ok(Self { path: path.to_owned(), _lock: lock, revoked })
    }

    /// Retains the in-memory denial even when persistence fails. The caller must return an error.
    pub fn revoke(&mut self, viewer: &str) -> Result<(), &'static str> {
        if !identifier(viewer) || (!self.revoked.contains(viewer) && self.revoked.len() >= MAX_REVOKED) {
            return Err("revocation state capacity exceeded");
        }
        self.revoked.insert(viewer.to_owned());
        // Reject a deleted/replaced state file instead of silently reinitializing it.
        private_read(&self.path, MAX_BYTES)?;
        let bytes = serde_json::to_vec(&Snapshot {
            version: 1, revoked: self.revoked.iter().cloned().collect(),
        }).map_err(|_| "revocation serialization failed")?;
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).map_err(|_| "state entropy unavailable")?;
        let suffix: String = nonce.iter().map(|b| format!("{b:02x}")).collect();
        let parent = self.path.parent().ok_or("invalid state path")?;
        let temporary = parent.join(format!(".nxrs-revocations-{suffix}.tmp"));
        let result = (|| {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            options.mode(0o600);
            let mut file = options.open(&temporary).map_err(|_| "state staging failed")?;
            file.write_all(&bytes).map_err(|_| "state write failed")?;
            file.sync_all().map_err(|_| "state sync failed")?;
            fs::rename(&temporary, &self.path).map_err(|_| "state replacement failed")?;
            File::open(parent).and_then(|f| f.sync_all()).map_err(|_| "state directory sync failed")?;
            Ok(())
        })();
        if result.is_err() { let _ = fs::remove_file(&temporary); }
        result
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    pub struct Fixture { pub directory: PathBuf, pub path: PathBuf }
    impl Fixture {
        pub fn new() -> Self {
            let mut nonce = [0u8; 16]; getrandom::fill(&mut nonce).unwrap();
            let name: String = nonce.iter().map(|b| format!("{b:02x}")).collect();
            let directory = std::env::temp_dir().join(format!("nxrs-state-{name}"));
            fs::create_dir(&directory).unwrap();
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
            let path = directory.join("revocations.json");
            fs::write(&path, b"{\"version\":1,\"revoked\":[]}").unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
            Self { directory, path }
        }
    }
    impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.directory); } }

    #[test] fn durable_idempotent_snapshot_and_exclusive_owner() {
        let fixture = Fixture::new();
        let mut store = Store::open(&fixture.path).unwrap();
        assert!(Store::open(&fixture.path).is_err());
        store.revoke("viewer_one").unwrap(); store.revoke("viewer_one").unwrap();
        assert_eq!(fs::metadata(&fixture.path).unwrap().mode() & 0o777, 0o600);
        drop(store);
        let reopened = Store::open(&fixture.path).unwrap();
        assert_eq!(reopened.revoked.len(), 1);
        assert!(reopened.revoked.contains("viewer_one"));
    }
    #[test] fn missing_corrupt_duplicate_and_unknown_version_fail_closed() {
        for contents in ["{", "{\"version\":2,\"revoked\":[]}",
            "{\"version\":1,\"revoked\":[\"a\",\"a\"]}",
            "{\"version\":1,\"revoked\":[\"../a\"]}"] {
            let fixture = Fixture::new(); fs::write(&fixture.path, contents).unwrap();
            assert!(Store::open(&fixture.path).is_err());
        }
        let fixture = Fixture::new(); fs::remove_file(&fixture.path).unwrap();
        assert!(Store::open(&fixture.path).is_err());
    }
    #[test] fn unsafe_permissions_symlinks_and_hardlinks_fail_closed() {
        let fixture = Fixture::new();
        fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(Store::open(&fixture.path).is_err());
        fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o600)).unwrap();
        let other = fixture.directory.join("other.json"); fs::rename(&fixture.path, &other).unwrap();
        symlink(&other, &fixture.path).unwrap(); assert!(Store::open(&fixture.path).is_err());
        fs::remove_file(&fixture.path).unwrap(); fs::hard_link(&other, &fixture.path).unwrap();
        assert!(Store::open(&fixture.path).is_err());
    }
    #[test] fn opened_file_is_checked_even_when_its_path_is_replaced() {
        let fixture = Fixture::new();
        let file = File::open(&fixture.path).unwrap();
        let old = fixture.directory.join("old.json");
        fs::rename(&fixture.path, &old).unwrap();
        fs::write(&fixture.path, b"replacement").unwrap();
        fs::set_permissions(&fixture.path, fs::Permissions::from_mode(0o600)).unwrap();
        fs::set_permissions(&old, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(private_read(&fixture.path, MAX_BYTES).is_ok());
        assert!(read_opened(file, MAX_BYTES).is_err());
    }
    #[test] fn failed_write_keeps_memory_denied_and_retry_can_persist() {
        let fixture = Fixture::new(); let mut store = Store::open(&fixture.path).unwrap();
        let backup = fixture.directory.join("backup"); fs::rename(&fixture.path, &backup).unwrap();
        assert!(store.revoke("viewer_one").is_err());
        assert!(store.revoked.contains("viewer_one"));
        fs::rename(&backup, &fixture.path).unwrap(); store.revoke("viewer_one").unwrap();
        drop(store); assert!(Store::open(&fixture.path).unwrap().revoked.contains("viewer_one"));
    }
}

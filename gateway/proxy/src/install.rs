//! Explicit publication of an administrator-reviewed proxy pair.
//! No service operation, network request, key generation or automatic approval.
use crate::admission;
use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

type Result<T> = std::result::Result<T, &'static str>;
const MAX_BINARY: u64 = 64 * 1024 * 1024;
const MAX_QUALIFICATION: u64 = 1024 * 1024;
const MAX_STAGES: usize = 32;
const MAX_VERSIONS: usize = 256;
static NEXT: AtomicU64 = AtomicU64::new(0);

fn require(value: bool, category: &'static str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(category)
    }
}

fn read(path: &Path, maximum: u64, owner: Option<u32>) -> Result<Vec<u8>> {
    let named = std::fs::symlink_metadata(path).map_err(|_| "import-file")?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "import-file")?;
    let before = file.metadata().map_err(|_| "import-file")?;
    require(
        before.is_file()
            && before.nlink() == 1
            && before.len() <= maximum
            && before.mode() & 0o6022 == 0
            && owner.is_none_or(|uid| before.uid() == uid)
            && admission::identity(&named) == admission::identity(&before),
        "import-metadata",
    )?;
    let mut raw = Vec::new();
    (&file)
        .take(maximum + 1)
        .read_to_end(&mut raw)
        .map_err(|_| "import-read")?;
    let after = file.metadata().map_err(|_| "import-file")?;
    let named = std::fs::symlink_metadata(path).map_err(|_| "import-file")?;
    require(
        raw.len() as u64 == before.len()
            && admission::identity(&before) == admission::identity(&after)
            && admission::identity(&after) == admission::identity(&named),
        "import-changed",
    )?;
    Ok(raw)
}

struct Pair {
    approval: Vec<u8>,
    qualification: Vec<u8>,
    binary: Vec<u8>,
    digest: String,
    policy: admission::Policy,
}
impl Pair {
    fn read(
        directory: &Path,
        approval_digest: &str,
        qualification_digest: &str,
        now: u64,
    ) -> Result<Self> {
        admission::absolute(directory)?;
        require(
            admission::hash_text(approval_digest) && admission::hash_text(qualification_digest),
            "review-digest",
        )?;
        let approval = read(&directory.join("review.json"), 8192, None)?;
        require(
            admission::sha(&approval) == approval_digest,
            "review-digest",
        )?;
        let policy = admission::policy(&approval, now)?;
        require(
            policy.qualification_sha256 == qualification_digest,
            "qualification-digest",
        )?;
        let qualification = read(
            &directory.join("qualification.json"),
            MAX_QUALIFICATION,
            None,
        )?;
        require(
            admission::sha(&qualification) == qualification_digest,
            "qualification-digest",
        )?;
        // This is an evidence document, not authority inferred from its booleans.
        let _: serde_json::Value =
            serde_json::from_slice(&qualification).map_err(|_| "qualification-json")?;
        let binary = read(&directory.join("nginx"), MAX_BINARY, None)?;
        require(
            !binary.is_empty() && admission::sha(&binary) == policy.binary_sha256,
            "binary-digest",
        )?;
        Ok(Self {
            approval,
            qualification,
            binary,
            digest: approval_digest.to_owned(),
            policy,
        })
    }
    fn files(&self) -> [(&str, &[u8], u32); 3] {
        [
            ("approval.json", &self.approval, 0o644),
            ("qualification.json", &self.qualification, 0o600),
            ("nginx", &self.binary, 0o755),
        ]
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Expected {
    Absent,
    Approval(String),
}
impl Expected {
    fn parse(value: String) -> Result<Self> {
        if value == "none" {
            Ok(Self::Absent)
        } else {
            require(admission::hash_text(&value), "expected-selection")?;
            Ok(Self::Approval(value))
        }
    }
    fn matches(&self, actual: Option<&str>) -> bool {
        match (self, actual) {
            (Self::Absent, None) => true,
            (Self::Approval(expected), Some(actual)) => expected == actual,
            _ => false,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Activation {
    Selected,
    AlreadySelected,
}

struct Store {
    root: PathBuf,
    owner: u32,
    _lock: File,
}
impl Store {
    fn open(root: &Path) -> Result<Self> {
        admission::parents(&root.join("active.json"))?;
        let lock = nxrs_gateway_private_file::lock(&root.join(".install.lock"))
            .map_err(|_| "install-lock")?;
        require(
            lock.metadata().map_err(|_| "install-lock")?.uid() == 0,
            "install-lock-owner",
        )?;
        let store = Self {
            root: root.to_owned(),
            owner: 0,
            _lock: lock,
        };
        let versions = store.root.join("versions");
        match std::fs::symlink_metadata(&versions) {
            Ok(_) => store.directory(&versions)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                std::fs::DirBuilder::new()
                    .mode(0o755)
                    .create(&versions)
                    .map_err(|_| "versions-create")?;
                sync(&store.root)?;
            }
            _ => return Err("versions-directory"),
        }
        Ok(store)
    }
    fn directory(&self, path: &Path) -> Result<()> {
        let m = std::fs::symlink_metadata(path).map_err(|_| "version-directory")?;
        require(
            m.is_dir() && m.uid() == self.owner && m.mode() & 0o022 == 0,
            "version-directory",
        )
    }
    fn current(&self) -> Result<Option<String>> {
        let path = self.root.join("active.json");
        match std::fs::symlink_metadata(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Ok(_) => {
                let raw = read(&path, 1024, Some(self.owner))?;
                Ok(Some(admission::selected_policy(&path, &raw)?.1))
            }
            _ => Err("selection-read"),
        }
    }
    fn expect(&self, pair: &Pair, expected: &Expected) -> Result<bool> {
        let current = self.current()?;
        if current.as_deref() == Some(pair.digest.as_str()) {
            return Ok(true);
        }
        require(expected.matches(current.as_deref()), "stale-selection")?;
        Ok(false)
    }
    fn verify(&self, directory: &Path, pair: &Pair) -> Result<()> {
        self.directory(directory)?;
        let mut names = BTreeSet::new();
        for entry in std::fs::read_dir(directory).map_err(|_| "version-inventory")? {
            let entry = entry.map_err(|_| "version-inventory")?;
            require(names.len() < 3, "version-inventory")?;
            names.insert(entry.file_name());
        }
        require(
            names
                == ["approval.json", "qualification.json", "nginx"]
                    .into_iter()
                    .map(Into::into)
                    .collect(),
            "version-inventory",
        )?;
        for (name, bytes, mode) in pair.files() {
            let path = directory.join(name);
            let m = std::fs::symlink_metadata(&path).map_err(|_| "version-file")?;
            require(m.mode() & 0o7777 == mode, "version-mode")?;
            let actual = read(&path, bytes.len() as u64, Some(self.owner))?;
            require(actual == bytes, "version-digest")?;
        }
        Ok(())
    }
    fn budget(&self) -> Result<()> {
        let mut stages = 0;
        let mut versions = 0;
        for entry in
            std::fs::read_dir(self.root.join("versions")).map_err(|_| "version-inventory")?
        {
            let entry = entry.map_err(|_| "version-inventory")?;
            let name = entry.file_name();
            let name = name.to_str().ok_or("version-name")?;
            self.directory(&entry.path())?;
            if name.starts_with(".stage-") {
                stages += 1;
            } else {
                require(admission::hash_text(name), "version-name")?;
                versions += 1;
            }
            require(
                stages < MAX_STAGES && versions < MAX_VERSIONS,
                "version-budget",
            )?;
        }
        Ok(())
    }
    fn stage(&mut self, pair: &Pair) -> Result<PathBuf> {
        self.stage_with_sync(pair, sync)
    }
    fn stage_with_sync(
        &mut self,
        pair: &Pair,
        directory_sync: fn(&Path) -> Result<()>,
    ) -> Result<PathBuf> {
        let versions = self.root.join("versions");
        let destination = versions.join(&pair.digest);
        match std::fs::symlink_metadata(&destination) {
            Ok(_) => {
                self.verify(&destination, pair)?;
                // A prior process could have died after renaming this complete
                // version but before syncing its parent. Establish dependency
                // durability again before publishing or confirming a selector.
                for (name, _, _) in pair.files() {
                    sync(&destination.join(name))?;
                }
                directory_sync(&destination)?;
                directory_sync(&versions)?;
                return Ok(destination);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            _ => return Err("version-directory"),
        }
        self.budget()?;
        let staging = (0..MAX_STAGES)
            .find_map(|_| {
                let path = versions.join(format!(
                    ".stage-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
                match std::fs::DirBuilder::new().mode(0o700).create(&path) {
                    Ok(()) => Some(Ok(path)),
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => None,
                    Err(_) => Some(Err("staging-create")),
                }
            })
            .ok_or("staging-budget")??;
        for (name, bytes, mode) in pair.files() {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(staging.join(name))
                .map_err(|_| "staging-file")?;
            file.write_all(bytes).map_err(|_| "staging-write")?;
            file.set_permissions(std::fs::Permissions::from_mode(mode))
                .map_err(|_| "staging-mode")?;
            file.sync_all().map_err(|_| "staging-sync")?;
        }
        self.verify(&staging, pair)?;
        std::fs::set_permissions(&staging, std::fs::Permissions::from_mode(0o755))
            .map_err(|_| "staging-mode")?;
        directory_sync(&staging)?;
        // All writers hold the persistent lock; unprivileged users cannot write
        // these parents. An existing version is never removed or repaired here.
        std::fs::rename(&staging, &destination).map_err(|_| "version-publish")?;
        directory_sync(&versions)?;
        self.verify(&destination, pair)?;
        Ok(destination)
    }
    fn activate(&mut self, pair: &Pair, expected: &Expected) -> Result<Activation> {
        self.activate_with_sync(pair, expected, sync)
    }
    fn activate_with_sync(
        &mut self,
        pair: &Pair,
        expected: &Expected,
        directory_sync: fn(&Path) -> Result<()>,
    ) -> Result<Activation> {
        let already = self.expect(pair, expected)?;
        self.verify(&self.root.join("versions").join(&pair.digest), pair)?;
        if already {
            directory_sync(&self.root).map_err(|_| "selection-visible-durability-uncertain")?;
            return Ok(Activation::AlreadySelected);
        }
        let raw = admission::selection_bytes(&pair.digest)?;
        // One fixed temporary name is safe under the held writer lock. A crash
        // leaves only an owned bounded regular file; validate before retiring it.
        let temporary = self.root.join(".active.pending");
        if temporary.try_exists().map_err(|_| "selection-temporary")? {
            let _ = read(&temporary, 1024, Some(self.owner))?;
            std::fs::remove_file(&temporary).map_err(|_| "selection-temporary")?;
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&temporary)
            .map_err(|_| "selection-temporary")?;
        file.write_all(&raw).map_err(|_| "selection-write")?;
        file.set_permissions(std::fs::Permissions::from_mode(0o644))
            .map_err(|_| "selection-mode")?;
        file.sync_all().map_err(|_| "selection-sync")?;
        drop(file);
        require(
            read(&temporary, 1024, Some(self.owner))? == raw,
            "selection-write",
        )?;
        std::fs::rename(&temporary, self.root.join("active.json"))
            .map_err(|_| "selection-publish")?;
        directory_sync(&self.root).map_err(|_| "selection-visible-durability-uncertain")?;
        Ok(Activation::Selected)
    }
}

fn sync(path: &Path) -> Result<()> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| "directory-sync")
}

#[derive(Debug)]
struct Arguments {
    review: PathBuf,
    destination: PathBuf,
    approval: String,
    qualification: String,
    expected: Expected,
}
fn arguments(mut args: impl Iterator<Item = String>) -> Result<Arguments> {
    let mut review = None;
    let mut destination = None;
    let mut approval = None;
    let mut qualification = None;
    let mut expected = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--review-dir" | "--destination" => {
                let slot = if arg == "--review-dir" {
                    &mut review
                } else {
                    &mut destination
                };
                require(slot.is_none(), "install-arguments")?;
                let value = PathBuf::from(args.next().ok_or("install-arguments")?);
                admission::absolute(&value)?;
                *slot = Some(value);
            }
            "--review-sha256" | "--qualification-sha256" => {
                let slot = if arg == "--review-sha256" {
                    &mut approval
                } else {
                    &mut qualification
                };
                require(slot.is_none(), "install-arguments")?;
                let value = args.next().ok_or("install-arguments")?;
                require(admission::hash_text(&value), "review-digest")?;
                *slot = Some(value);
            }
            "--expected-active" => {
                require(expected.is_none(), "install-arguments")?;
                expected = Some(Expected::parse(args.next().ok_or("install-arguments")?)?);
            }
            _ => return Err("install-arguments"),
        }
    }
    Ok(Arguments {
        review: review.ok_or("install-arguments")?,
        destination: destination.ok_or("install-arguments")?,
        approval: approval.ok_or("install-arguments")?,
        qualification: qualification.ok_or("install-arguments")?,
        expected: expected.ok_or("install-arguments")?,
    })
}

pub(super) fn run() -> Result<()> {
    require(
        std::fs::metadata("/proc/self")
            .map_err(|_| "installer-root")?
            .uid()
            == 0,
        "installer-root",
    )?;
    let args = arguments(std::env::args().skip(1))?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "clock")?
        .as_secs();
    let pair = Pair::read(&args.review, &args.approval, &args.qualification, now)?;
    let mut store = Store::open(&args.destination)?;
    store.expect(&pair, &args.expected)?;
    let version = store.stage(&pair)?;
    admission::verify_pair_graph(&version.join("nginx"), &pair.policy)?;
    let result = store.activate(&pair, &args.expected)?;
    match result {
        Activation::Selected => {
            println!("Reviewed proxy pair selected; service restart remains an operator action.")
        }
        Activation::AlreadySelected => {
            println!("Reviewed proxy pair already selected; directory durability confirmed.")
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{atomic::AtomicBool, mpsc, Arc};
    struct Observer {
        stop: Arc<AtomicBool>,
        thread: Option<std::thread::JoinHandle<usize>>,
    }
    impl Observer {
        fn join(mut self) -> usize {
            self.stop.store(true, Ordering::Relaxed);
            self.thread.take().unwrap().join().unwrap()
        }
    }
    impl Drop for Observer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Relaxed);
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }
    struct Fixture {
        root: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "nxrs-proxy-pair-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .unwrap();
            std::fs::create_dir(root.join("versions")).unwrap();
            Self {
                root: root.canonicalize().unwrap(),
            }
        }
        fn store(&self) -> Result<Store> {
            let lock = nxrs_gateway_private_file::lock(&self.root.join(".install.lock"))
                .map_err(|_| "install-lock")?;
            // Only this private test fixture constructs a non-root store. The
            // production constructor and CLI require protected root ownership.
            Ok(Store {
                root: self.root.clone(),
                owner: std::fs::metadata(&self.root).unwrap().uid(),
                _lock: lock,
            })
        }
        fn pair(&self, label: &str) -> Pair {
            let binary = format!("owned test-only program {label}").into_bytes();
            let qualification = br#"{"scope":"test-only-public-evidence"}"#.to_vec();
            let source: serde_json::Value = serde_json::from_slice(include_bytes!(
                "../../../security/gateway-proxy-dependencies.json"
            ))
            .unwrap();
            let policy = serde_json::json!({"schema":1,"scope":"operator-approved-minimal-camera-proxy",
                "source_policy_sha256":admission::sha(include_bytes!("../../../security/gateway-proxy-dependencies.json")),
                "qualification_sha256":admission::sha(&qualification),"binary_sha256":admission::sha(&binary),
                "reviewed_at":source["advisory_review"]["reviewed_at_unix"],"valid_until":source["advisory_review"]["valid_until_unix"],
                "libraries":{"loader":"c".repeat(64),"libc":"d".repeat(64),"crypto":"e".repeat(64),"ssl":"f".repeat(64)}});
            let approval = serde_json::to_vec(&policy).unwrap();
            let digest = admission::sha(&approval);
            let now = source["advisory_review"]["reviewed_at_unix"]
                .as_u64()
                .unwrap()
                + 1;
            let policy = admission::policy(&approval, now).unwrap();
            Pair {
                approval,
                qualification,
                binary,
                digest,
                policy,
            }
        }
        fn assert_selected(&self, pair: &Pair) {
            let raw = std::fs::read(self.root.join("active.json")).unwrap();
            let (policy, digest) =
                admission::selected_policy(&self.root.join("active.json"), &raw).unwrap();
            assert_eq!(digest, pair.digest);
            assert_eq!(
                std::fs::read(policy.parent().unwrap().join("nginx")).unwrap(),
                pair.binary
            );
            assert_eq!(std::fs::read(policy).unwrap(), pair.approval);
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn publication_preserves_complete_old_pair_and_retained_reader_identity() {
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        assert_eq!(store.current().unwrap(), None);
        store.stage(&a).unwrap();
        assert_eq!(
            store.activate(&a, &Expected::Absent).unwrap(),
            Activation::Selected
        );
        fixture.assert_selected(&a);
        let previous = std::fs::read(fixture.root.join("active.json")).unwrap();
        store.stage(&b).unwrap();
        assert_eq!(store.current().unwrap().as_deref(), Some(a.digest.as_str()));
        store
            .activate(&b, &Expected::Approval(a.digest.clone()))
            .unwrap();
        fixture.assert_selected(&b);
        let (old_path, old_digest) =
            admission::selected_policy(&fixture.root.join("active.json"), &previous).unwrap();
        assert_eq!(old_digest, a.digest);
        assert_eq!(std::fs::read(old_path).unwrap(), a.approval);
        assert_eq!(
            store
                .activate(&b, &Expected::Approval(a.digest.clone()))
                .unwrap(),
            Activation::AlreadySelected
        );
        fixture.assert_selected(&b);
    }
    #[test]
    fn stale_updates_and_changed_existing_version_never_modify_the_selector() {
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        store.stage(&a).unwrap();
        store.activate(&a, &Expected::Absent).unwrap();
        let version = store.stage(&b).unwrap();
        assert_eq!(
            store.activate(&b, &Expected::Absent).unwrap_err(),
            "stale-selection"
        );
        fixture.assert_selected(&a);
        std::fs::write(version.join("nginx"), b"changed program").unwrap();
        assert!(store.stage(&b).is_err());
        assert!(store
            .activate(&b, &Expected::Approval(a.digest.clone()))
            .is_err());
        fixture.assert_selected(&a);
    }
    #[test]
    fn interrupted_partial_stage_and_empty_pending_file_recover_without_selecting_partial_data() {
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        store.stage(&a).unwrap();
        store.activate(&a, &Expected::Absent).unwrap();
        let partial = fixture.root.join("versions/.stage-interrupted");
        std::fs::create_dir(&partial).unwrap();
        std::fs::write(partial.join("nginx"), b"partial program").unwrap();
        let pending = fixture.root.join(".active.pending");
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&pending)
            .unwrap();
        fixture.assert_selected(&a);
        store.stage(&b).unwrap();
        store
            .activate(&b, &Expected::Approval(a.digest.clone()))
            .unwrap();
        fixture.assert_selected(&b);
        assert_eq!(
            std::fs::read(partial.join("nginx")).unwrap(),
            b"partial program"
        );
        assert!(!pending.exists());
    }
    #[test]
    fn final_sync_failure_reports_visibility_and_retry_confirms_without_rollback() {
        fn fail(_: &Path) -> Result<()> {
            Err("synthetic-directory-sync-failure")
        }
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        store.stage(&a).unwrap();
        store.activate(&a, &Expected::Absent).unwrap();
        store.stage(&b).unwrap();
        assert_eq!(
            store
                .activate_with_sync(&b, &Expected::Approval(a.digest.clone()), fail)
                .unwrap_err(),
            "selection-visible-durability-uncertain"
        );
        fixture.assert_selected(&b);
        assert_eq!(
            store
                .activate(&b, &Expected::Approval(a.digest.clone()))
                .unwrap(),
            Activation::AlreadySelected
        );
        fixture.assert_selected(&b);
    }
    #[test]
    fn reused_complete_version_requires_directory_durability_before_selection() {
        fn fail(_: &Path) -> Result<()> {
            Err("synthetic-dependency-sync-failure")
        }
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        store.stage(&a).unwrap();
        store.activate(&a, &Expected::Absent).unwrap();
        let version = store.stage(&b).unwrap();
        assert_eq!(
            store.stage_with_sync(&b, fail).unwrap_err(),
            "synthetic-dependency-sync-failure"
        );
        fixture.assert_selected(&a);
        assert_eq!(std::fs::read(version.join("nginx")).unwrap(), b.binary);
        store.stage(&b).unwrap();
        store
            .activate(&b, &Expected::Approval(a.digest.clone()))
            .unwrap();
        fixture.assert_selected(&b);
    }
    #[test]
    fn writer_lock_excludes_another_writer_and_is_retained_after_release() {
        let fixture = Fixture::new();
        let first = fixture.store().unwrap();
        let before = std::fs::metadata(fixture.root.join(".install.lock")).unwrap();
        assert!(fixture.store().is_err());
        drop(first);
        let _second = fixture.store().unwrap();
        let after = std::fs::metadata(fixture.root.join(".install.lock")).unwrap();
        assert_eq!(before.ino(), after.ino());
        assert_eq!(after.len(), 0);
    }
    #[test]
    fn simultaneous_reader_and_writer_observe_whole_retained_pairs() {
        let fixture = Fixture::new();
        let mut store = fixture.store().unwrap();
        let a = fixture.pair("A");
        let b = fixture.pair("B");
        store.stage(&a).unwrap();
        store.activate(&a, &Expected::Absent).unwrap();
        store.stage(&b).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let root = fixture.root.clone();
        let done = stop.clone();
        let (send, receive) = mpsc::channel();
        let reader = std::thread::spawn(move || {
            let mut observations = 0;
            while !done.load(Ordering::Relaxed) {
                let raw = std::fs::read(root.join("active.json")).unwrap();
                let (policy, digest) =
                    admission::selected_policy(&root.join("active.json"), &raw).unwrap();
                let raw = std::fs::read(&policy).unwrap();
                assert_eq!(admission::sha(&raw), digest);
                let value: serde_json::Value = serde_json::from_slice(&raw).unwrap();
                let binary = std::fs::read(policy.parent().unwrap().join("nginx")).unwrap();
                assert_eq!(value["binary_sha256"], admission::sha(&binary));
                observations += 1;
                if observations == 1 {
                    send.send(()).unwrap();
                }
                std::thread::sleep(std::time::Duration::from_micros(50));
            }
            observations
        });
        let observer = Observer {
            stop: stop.clone(),
            thread: Some(reader),
        };
        receive
            .recv_timeout(std::time::Duration::from_secs(3))
            .unwrap();
        let mut previous = a.digest.clone();
        for index in 0..40 {
            let next = if index % 2 == 0 { &b } else { &a };
            store.activate(next, &Expected::Approval(previous)).unwrap();
            previous = next.digest.clone();
        }
        assert!(observer.join() > 0);
        fixture.assert_selected(&a);
    }
    #[test]
    fn incoming_bundle_requires_each_explicit_reviewed_digest_and_refuses_links_or_writable_sources(
    ) {
        let fixture = Fixture::new();
        let pair = fixture.pair("incoming");
        let directory = fixture.root.join("review");
        std::fs::create_dir(&directory).unwrap();
        for (name, data, _) in pair.files() {
            let name = if name == "approval.json" {
                "review.json"
            } else {
                name
            };
            std::fs::write(directory.join(name), data).unwrap();
        }
        let source: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../../security/gateway-proxy-dependencies.json"
        ))
        .unwrap();
        let now = source["advisory_review"]["reviewed_at_unix"]
            .as_u64()
            .unwrap()
            + 1;
        let q = admission::sha(&pair.qualification);
        assert_eq!(
            Pair::read(&directory, &pair.digest, &q, now)
                .unwrap()
                .digest,
            pair.digest
        );
        assert!(Pair::read(&directory, &"0".repeat(64), &q, now).is_err());
        assert!(Pair::read(&directory, &pair.digest, &"0".repeat(64), now).is_err());
        let original = directory.join("nginx");
        std::fs::rename(&original, directory.join("original")).unwrap();
        std::os::unix::fs::symlink("original", &original).unwrap();
        assert!(Pair::read(&directory, &pair.digest, &q, now).is_err());
        std::fs::remove_file(&original).unwrap();
        std::fs::hard_link(directory.join("original"), &original).unwrap();
        assert!(Pair::read(&directory, &pair.digest, &q, now).is_err());
        std::fs::remove_file(&original).unwrap();
        std::fs::rename(directory.join("original"), &original).unwrap();
        std::fs::set_permissions(&original, std::fs::Permissions::from_mode(0o666)).unwrap();
        assert!(Pair::read(&directory, &pair.digest, &q, now).is_err());
    }
    #[test]
    fn installer_arguments_require_explicit_review_qualification_destination_and_expected_selection(
    ) {
        let base = [
            "--review-dir",
            "/owned/review",
            "--destination",
            "/owned/installed",
            "--review-sha256",
            &"a".repeat(64),
            "--qualification-sha256",
            &"b".repeat(64),
            "--expected-active",
            "none",
        ];
        assert_eq!(
            arguments(base.iter().map(|v| v.to_string()))
                .unwrap()
                .expected,
            Expected::Absent
        );
        for extra in [
            ["--expected-active", "none"],
            ["--destination", "/other"],
            ["--run-service", "true"],
        ] {
            assert!(arguments(base.iter().chain(extra.iter()).map(|v| v.to_string())).is_err());
        }
        assert!(arguments(base[..8].iter().map(|v| v.to_string())).is_err());
        assert!(arguments(base.iter().map(|v| v.to_string()).map(
            |v| if v == "/owned/installed" {
                "/owned/../outside".to_owned()
            } else {
                v
            }
        ))
        .is_err());
        assert!(Expected::parse("../outside".to_owned()).is_err());
    }
    #[test]
    fn production_constructor_does_not_accept_unprotected_test_roots() {
        let fixture = Fixture::new();
        assert!(Store::open(&fixture.root).is_err());
        // The public factory refuses unsupported hosts or the root-only entry
        // point/required arguments before it can allocate an installation.
        assert!(crate::install_pair().is_err());
        assert!(!fixture.root.join("active.json").exists());
        if !cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            assert!(run().is_err());
        }
    }
}

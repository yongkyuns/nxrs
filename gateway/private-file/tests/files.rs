#![cfg(unix)]
#![forbid(unsafe_code)]
use nxrs_gateway_private_file::{lock, read, read_owned};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    os::unix::{
        fs::{symlink, OpenOptionsExt, PermissionsExt},
        net::UnixListener,
    },
    path::PathBuf,
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

// A concurrent subprocess fork can temporarily inherit a locked descriptor
// until exec closes it. Keep the last-owner Drop witness separate from the
// FIFO subprocess witness; production still uses nonblocking fail-closed locks.
static FORK_BARRIER: std::sync::Mutex<()> = std::sync::Mutex::new(());

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "nxrs-private-files-{}-{nonce}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        Self(directory)
    }
    fn file(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.0.join(name);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        file.write_all(bytes).unwrap();
        path
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn private_data_empty_and_exact_bound() {
    let fixture = Fixture::new();
    let path = fixture.file("config", b"synthetic private value");
    assert_eq!(read(&path, 23).unwrap(), b"synthetic private value");
    assert!(read(&path, 21).is_err());
    assert!(read(&path, u64::MAX).is_err());
    let empty = fixture.file("empty", b"");
    assert!(read(&empty, 0).unwrap().is_empty());
}

#[test]
fn expected_owner_is_checked_on_the_opened_private_file() {
    use std::os::unix::fs::MetadataExt;
    let fixture = Fixture::new();
    let path = fixture.file("owned", b"private owner witness");
    let owner = fs::metadata(&path).unwrap().uid();
    assert_eq!(
        read_owned(&path, 64, owner).unwrap(),
        b"private owner witness"
    );
    assert!(read_owned(&path, 64, owner.wrapping_add(1)).is_err());
    assert_eq!(fs::read(&path).unwrap(), b"private owner witness");
}

#[test]
fn invalid_paths_and_missing_files_fail_closed() {
    let fixture = Fixture::new();
    for path in [
        PathBuf::from("relative"),
        fixture.0.join("../other"),
        fixture.0.join("absent"),
    ] {
        assert!(read(&path, 8192).is_err());
    }
    assert!(lock(&PathBuf::from("relative")).is_err());
    assert!(lock(&fixture.0.join("../other")).is_err());
}

#[test]
fn permissions_links_and_special_files_are_rejected() {
    let fixture = Fixture::new();
    let path = fixture.file("private", b"private");
    for mode in [0o640, 0o604, 0o610, 0o601, 0o644] {
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
        assert!(read(&path, 8192).is_err());
    }
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let link = fixture.0.join("link");
    symlink(&path, &link).unwrap();
    assert!(read(&link, 8192).is_err());
    fs::remove_file(&link).unwrap();
    fs::hard_link(&path, &link).unwrap();
    assert!(read(&path, 8192).is_err());
    assert!(read(&link, 8192).is_err());
    assert!(read(&fixture.0, 8192).is_err());
    let socket = fixture.0.join("socket");
    let _listener = UnixListener::bind(&socket).unwrap();
    assert!(read(&socket, 8192).is_err());
    assert!(read(&PathBuf::from("/dev/null"), 8192).is_err());
}

#[test]
fn lock_is_private_exclusive_and_reusable_after_last_owner_drop() {
    let _fork_barrier = FORK_BARRIER.lock().unwrap();
    let fixture = Fixture::new();
    let path = fixture.0.join("lock");
    let owner = lock(&path).unwrap();
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(lock(&path).is_err());
    let duplicate = owner.try_clone().unwrap();
    drop(owner);
    assert!(lock(&path).is_err());
    drop(duplicate);
    assert!(lock(&path).is_ok());
}

#[test]
fn unsafe_lock_is_never_followed_truncated_or_repaired() {
    let fixture = Fixture::new();
    let path = fixture.file("nonempty", b"retain this evidence");
    assert!(lock(&path).is_err());
    assert_eq!(fs::read(&path).unwrap(), b"retain this evidence");
    let empty = fixture.file("empty", b"");
    let link = fixture.0.join("symlink");
    symlink(&empty, &link).unwrap();
    assert!(lock(&link).is_err());
    let hard = fixture.0.join("hardlink");
    fs::hard_link(&empty, &hard).unwrap();
    assert!(lock(&empty).is_err());
    assert!(lock(&hard).is_err());
    let public = fixture.file("public", b"");
    fs::set_permissions(&public, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(lock(&public).is_err());
    assert_eq!(
        fs::metadata(&public).unwrap().permissions().mode() & 0o777,
        0o644
    );
    assert!(lock(&fixture.0).is_err());
}

// A separate process plus a watchdog makes a regression to blocking FIFO open
// fail the test instead of hanging the entire qualification job.
#[test]
fn fifo_is_rejected_without_waiting_for_a_peer() {
    let _fork_barrier = FORK_BARRIER.lock().unwrap();
    let fixture = Fixture::new();
    let path = fixture.0.join("fifo");
    assert!(Command::new("mkfifo")
        .arg("-m")
        .arg("600")
        .arg(&path)
        .status()
        .unwrap()
        .success());
    for operation in ["read", "lock"] {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "fifo_probe", "--test-threads=1"])
            .env("NXRS_TEST_FIFO_PATH", &path)
            .env("NXRS_TEST_FIFO_OPERATION", operation)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            if Instant::now() >= deadline {
                child.kill().unwrap();
                let _ = child.wait();
                panic!("private file operation waited for a FIFO peer");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[test]
fn fifo_probe() {
    if let Some(path) = std::env::var_os("NXRS_TEST_FIFO_PATH") {
        let path = PathBuf::from(path);
        match std::env::var("NXRS_TEST_FIFO_OPERATION").unwrap().as_str() {
            "read" => assert!(read(&path, 8192).is_err()),
            "lock" => assert!(lock(&path).is_err()),
            _ => panic!("invalid test operation"),
        }
    }
}

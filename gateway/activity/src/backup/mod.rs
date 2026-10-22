//! Operator-only local encrypted backup/restore. No enrollment or denial state is archived.
mod manifest;
mod process;
mod restore;
#[cfg(test)]
mod tests;
use crate::{
    protection::{Access, Volume},
    store::{self, Settings, Store},
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs::{self},
    io::Read,
    os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt},
    path::{Component, Path, PathBuf},
};
use tokio::{
    sync::watch,
    time::{Duration, Instant},
};
use zeroize::Zeroizing;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Invalid,
    Io,
    Busy,
    Protection,
    Authentication,
    Repository,
    Native,
    Output,
    Timeout,
    Cancelled,
    Recovery,
}
impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
impl From<store::Error> for Error {
    fn from(error: store::Error) -> Self {
        match error {
            store::Error::Busy => Self::Busy,
            store::Error::Protection => Self::Protection,
            _ => Self::Invalid,
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid backup configuration or snapshot",
            Self::Io => "backup storage unavailable",
            Self::Busy => "recording or backup resources are busy; stop the whole group",
            Self::Protection => "protected non-root maintenance boundary required",
            Self::Authentication => "backup key rejected",
            Self::Repository => "backup repository identity mismatch",
            Self::Native => "backup tool refused the operation",
            Self::Output => "backup tool output limit exceeded",
            Self::Timeout => "backup operation deadline exceeded",
            Self::Cancelled => "backup operation cancelled",
            Self::Recovery => "interrupted restore requires explicit recovery",
        })
    }
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    storage: Settings,
    repository: PathBuf,
    repository_id: Option<String>,
    restic: PathBuf,
    restic_sha256: String,
    timeout_seconds: u64,
}
pub enum Operation {
    Init,
    Backup,
    Check,
    UnlockStale,
    Restore(String),
    Resume(String),
}
pub struct Receipt {
    pub repository_id: String,
    pub snapshot_id: Option<String>,
}
fn hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}
fn read_private(path: &Path, max: u64) -> Result<Vec<u8>, Error> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.mode() & 0o077 != 0
        || metadata.len() > max
    {
        return Err(Error::Invalid);
    }
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(Error::Invalid);
    }
    Ok(bytes)
}
fn private_dir(path: &Path, owner: u32) -> Result<(), Error> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir()
        || metadata.uid() != owner
        || metadata.mode() & 0o077 != 0
        || fs::canonicalize(path)? != path
    {
        return Err(Error::Protection);
    }
    Ok(())
}
impl Config {
    fn volume(&self) -> &Volume {
        self.storage
            .protection
            .as_ref()
            .expect("validated protected storage")
    }
    fn maintenance(&self) -> PathBuf {
        self.volume().maintenance()
    }
    fn load(path: &Path, operation: &Operation) -> Result<Self, Error> {
        let config: Self =
            serde_json::from_slice(&read_private(path, 65536)?).map_err(|_| Error::Invalid)?;
        config.storage.validate()?;
        if config.storage.protection.is_none() || !(1..=3600).contains(&config.timeout_seconds) {
            return Err(Error::Invalid);
        }
        for path in [&config.repository, &config.restic] {
            if !path.is_absolute()
                || path.components().any(|v| matches!(v, Component::ParentDir))
                || fs::canonicalize(path)? != *path
            {
                return Err(Error::Invalid);
            }
        }
        if config.repository.starts_with(&config.volume().mount)
            || config.volume().mount.starts_with(&config.repository)
            || !hex(&config.restic_sha256, 64)
        {
            return Err(Error::Invalid);
        }
        match operation {
            Operation::Init if config.repository_id.is_some() => return Err(Error::Invalid),
            Operation::Init => {}
            _ if !config.repository_id.as_deref().is_some_and(|v| hex(v, 64)) => {
                return Err(Error::Invalid)
            }
            _ => {}
        }
        if let Operation::Restore(id) | Operation::Resume(id) = operation {
            if !hex(id, 64) {
                return Err(Error::Invalid);
            }
        }
        config.storage.verify_protection()?;
        let owner = config.volume().operator_uid()?;
        private_dir(&config.maintenance(), owner)?;
        private_dir(&config.storage.directory, owner)?;
        private_dir(&config.repository, owner)?;
        config.verify_binary()?;
        // An unprivileged writer must not swap the verified native executable
        // between hashing and exec. Parent names are root-owned/non-writable;
        // sticky root-owned temporary ancestors are allowed for owned CI fixtures.
        if fs::symlink_metadata(&config.restic)?.uid() != 0 {
            return Err(Error::Protection);
        }
        for parent in config.restic.ancestors().skip(1) {
            let metadata = fs::symlink_metadata(parent)?;
            if !metadata.is_dir()
                || metadata.uid() != 0
                || (metadata.mode() & 0o022 != 0 && metadata.mode() & 0o1000 == 0)
            {
                return Err(Error::Protection);
            }
        }
        Ok(config)
    }
    fn verify_binary(&self) -> Result<(), Error> {
        // Exact reviewed official artifacts. Runtime identity does not select the implementation.
        const ALLOWED: [&str; 2] = [
            "06582569ff2f10e1935a6f12187c76db02f0bae99e6e54098f2cdc374766d768",
            "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639",
        ];
        if !ALLOWED.contains(&self.restic_sha256.as_str()) {
            return Err(Error::Invalid);
        }
        let mut file = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&self.restic)?;
        let metadata = file.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.mode() & 0o6022 != 0
            || metadata.len() > 128 * 1024 * 1024
        {
            return Err(Error::Invalid);
        }
        let mut hash = Sha256::new();
        let mut buffer = [0; 65536];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
        }
        if format!("{:x}", hash.finalize()) != self.restic_sha256 {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}
async fn key(stop: &watch::Receiver<bool>) -> Result<Zeroizing<Vec<u8>>, Error> {
    let mut input = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open("/dev/stdin")?;
    if !input.metadata()?.file_type().is_fifo() {
        return Err(Error::Invalid);
    }
    let deadline = Instant::now() + Duration::from_secs(1);
    let mut bytes = Zeroizing::new(Vec::new());
    loop {
        if *stop.borrow() {
            return Err(Error::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(Error::Timeout);
        }
        let mut chunk = Zeroizing::new([0; 66]);
        match input.read(chunk.as_mut_slice()) {
            Ok(0) => break,
            Ok(count) => {
                bytes.extend_from_slice(&chunk[..count]);
                if bytes.len() > 65 {
                    return Err(Error::Invalid);
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                tokio::time::sleep(Duration::from_millis(10)).await
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(_) => return Err(Error::Io),
        }
    }
    if bytes.last() == Some(&b'\n') {
        bytes.pop();
    }
    if !std::str::from_utf8(&bytes).is_ok_and(|v| hex(v, 64)) {
        return Err(Error::Invalid);
    }
    Ok(bytes)
}
/// All commands require a declared protected volume and an unprivileged recording owner.
pub async fn execute(
    path: &Path,
    operation: Operation,
    stop: watch::Receiver<bool>,
) -> Result<Receipt, Error> {
    if *stop.borrow() {
        return Err(Error::Cancelled);
    }
    let config = Config::load(path, &operation)?;
    let key = key(&stop).await?;
    let _lease = config.volume().lease(Access::Maintenance)?;
    config.storage.verify_protection()?;
    let temporary = config.maintenance().join("temporary");
    if !temporary.exists() {
        fs::DirBuilder::new().mode(0o700).create(&temporary)?;
    }
    private_dir(&temporary, config.volume().operator_uid()?)?;
    let restic = process::Restic {
        config: &config,
        key: &key,
        deadline: Instant::now() + Duration::from_secs(config.timeout_seconds),
        stop,
    };
    if matches!(operation, Operation::Init) {
        config.volume().require_no_restore()?;
        if fs::read_dir(&config.repository)?.next().is_some() {
            return Err(Error::Invalid);
        }
        restic
            .command(&["init", "--repository-version", "2"])
            .await?;
        let id = restic.repository(None).await?;
        return Ok(Receipt {
            repository_id: id,
            snapshot_id: None,
        });
    }
    let id = restic.repository(config.repository_id.as_deref()).await?;
    let snapshot = match operation {
        Operation::Backup => {
            config.volume().require_no_restore()?;
            let _frozen = Store::frozen(&config.storage)?;
            let bytes = restic
                .command(&[
                    "backup",
                    "--force",
                    "--one-file-system",
                    "--read-concurrency",
                    "2",
                    "--host",
                    "nxrs-activity",
                    "activity",
                ])
                .await?;
            config.storage.verify_protection()?;
            let snapshot = bytes
                .split(|b| *b == b'\n')
                .filter_map(|line| serde_json::from_slice::<serde_json::Value>(line).ok())
                .find(|value| value["message_type"] == "summary")
                .and_then(|value| value["snapshot_id"].as_str().map(String::from))
                .filter(|value| hex(value, 64))
                .ok_or(Error::Native)?;
            Some(snapshot)
        }
        Operation::Check => {
            config.volume().require_no_restore()?;
            restic.check().await?;
            None
        }
        Operation::UnlockStale => {
            restic.command(&["unlock"]).await?;
            None
        }
        Operation::Restore(snapshot) => {
            restore::run(&config, &restic, &snapshot, false).await?;
            Some(snapshot)
        }
        Operation::Resume(snapshot) => {
            restore::run(&config, &restic, &snapshot, true).await?;
            Some(snapshot)
        }
        Operation::Init => return Err(Error::Invalid),
    };
    Ok(Receipt {
        repository_id: id,
        snapshot_id: snapshot,
    })
}

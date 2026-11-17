//! Single-writer Matter blob snapshots on a trusted private local filesystem.
//!
//! Initialization is explicit. Opening missing/corrupt state never produces an
//! uncommissioned node. Each blob mutation replaces one bounded snapshot after
//! file sync, then syncs its directory. A storage failure poisons the owner until
//! restart, including ambiguous failures after rename. No key or value is logged.
use crate::platform;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

const MAGIC: &[u8; 8] = b"NXMAT\0\0\x01";
const STATE: &str = "state.bin";
const LOCK: &str = "writer.lock";
const NEXT: &str = "state.next";
pub const MAX_VALUE_BYTES: usize = 65_536;
pub const MAX_ENTRIES: usize = 512;
pub const MAX_SNAPSHOT_BYTES: usize = 1_048_576;

/// Intentionally contains no paths, keys, contents or underlying SDK errors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    InvalidPath,
    Unavailable,
    AlreadyOwned,
    Corrupt,
    Capacity,
    Changed,
    Failed,
    BufferTooSmall,
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Matter state {self:?}")
    }
}
impl std::error::Error for Error {}

#[derive(Clone, Default)]
struct Snapshot {
    generation: u64,
    entries: BTreeMap<u16, Vec<u8>>,
}

impl Snapshot {
    fn encode(&self) -> Result<Vec<u8>, Error> {
        let size = 8 + 8 + 2 + 32 + self.entries.values().map(|v| 6 + v.len()).sum::<usize>();
        if self.entries.len() > MAX_ENTRIES
            || size > MAX_SNAPSHOT_BYTES
            || self.entries.values().any(|v| v.len() > MAX_VALUE_BYTES)
        {
            return Err(Error::Capacity);
        }
        let mut bytes = Vec::with_capacity(size);
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&self.generation.to_le_bytes());
        bytes.extend_from_slice(&(self.entries.len() as u16).to_le_bytes());
        for (key, value) in &self.entries {
            bytes.extend_from_slice(&key.to_le_bytes());
            bytes.extend_from_slice(&(value.len() as u32).to_le_bytes());
            bytes.extend_from_slice(value);
        }
        let digest = Sha256::digest(&bytes);
        bytes.extend_from_slice(&digest);
        Ok(bytes)
    }

    fn decode(bytes: &[u8]) -> Result<Self, Error> {
        if !(50..=MAX_SNAPSHOT_BYTES).contains(&bytes.len()) {
            return Err(Error::Corrupt);
        }
        let (mut payload, digest) = bytes.split_at(bytes.len() - 32);
        if Sha256::digest(payload).as_slice() != digest || take(&mut payload, 8)? != MAGIC {
            return Err(Error::Corrupt);
        }
        let generation = u64::from_le_bytes(take(&mut payload, 8)?.try_into().unwrap());
        let count = u16::from_le_bytes(take(&mut payload, 2)?.try_into().unwrap()) as usize;
        if count > MAX_ENTRIES {
            return Err(Error::Corrupt);
        }
        let mut entries = BTreeMap::new();
        let mut previous = None;
        for _ in 0..count {
            let key = u16::from_le_bytes(take(&mut payload, 2)?.try_into().unwrap());
            let len = u32::from_le_bytes(take(&mut payload, 4)?.try_into().unwrap()) as usize;
            if len > MAX_VALUE_BYTES || previous.is_some_and(|p| p >= key) {
                return Err(Error::Corrupt);
            }
            entries.insert(key, take(&mut payload, len)?.to_vec());
            previous = Some(key);
        }
        if !payload.is_empty() {
            return Err(Error::Corrupt);
        }
        Ok(Self {
            generation,
            entries,
        })
    }
}

fn take<'a>(bytes: &mut &'a [u8], count: usize) -> Result<&'a [u8], Error> {
    let value = bytes.get(..count).ok_or(Error::Corrupt)?;
    *bytes = &bytes[count..];
    Ok(value)
}

fn read_state(path: &Path) -> Result<Vec<u8>, Error> {
    let file = platform::open_file(path, false, false).map_err(|_| Error::Unavailable)?;
    if file.metadata().map_err(|_| Error::Unavailable)?.len() > MAX_SNAPSHOT_BYTES as u64 {
        return Err(Error::Corrupt);
    }
    let mut bytes = Vec::new();
    file.take(MAX_SNAPSHOT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::Unavailable)?;
    if bytes.len() > MAX_SNAPSHOT_BYTES {
        return Err(Error::Corrupt);
    }
    Ok(bytes)
}

/// Owns the writer lock until all users have released this value. A node must
/// retire network sessions before dropping it. No background tasks are created.
pub struct Store {
    path: PathBuf,
    directory: File,
    lock: File,
    snapshot: Snapshot,
    digest: [u8; 32],
    failed: bool,
}

impl Store {
    /// Provision a new, nonexistent directory. Never resets an existing node.
    pub fn initialize(path: &Path) -> Result<Self, Error> {
        validate_path(path)?;
        platform::create_directory(path).map_err(|_| Error::Unavailable)?;
        let directory = platform::open_directory(path).map_err(|_| Error::Unavailable)?;
        let lock =
            platform::open_file(&path.join(LOCK), true, true).map_err(|_| Error::Unavailable)?;
        lock.try_lock().map_err(|_| Error::AlreadyOwned)?;
        let snapshot = Snapshot::default();
        let bytes = snapshot.encode()?;
        let mut file =
            platform::open_file(&path.join(STATE), true, true).map_err(|_| Error::Unavailable)?;
        file.write_all(&bytes)
            .and_then(|_| file.sync_all())
            .map_err(|_| Error::Failed)?;
        directory.sync_all().map_err(|_| Error::Failed)?;
        Ok(Self {
            path: path.to_owned(),
            directory,
            lock,
            snapshot,
            digest: Sha256::digest(&bytes).into(),
            failed: false,
        })
    }

    pub fn open(path: &Path) -> Result<Self, Error> {
        validate_path(path)?;
        let directory = platform::open_directory(path).map_err(|_| Error::Unavailable)?;
        let lock =
            platform::open_file(&path.join(LOCK), false, true).map_err(|_| Error::Unavailable)?;
        if lock.metadata().map_err(|_| Error::Unavailable)?.len() != 0 {
            return Err(Error::Corrupt);
        }
        lock.try_lock().map_err(|_| Error::AlreadyOwned)?;
        let bytes = read_state(&path.join(STATE))?;
        let snapshot = Snapshot::decode(&bytes)?;
        // An interrupted staging write cannot replace the last committed state.
        // Never promote an orphan, including when the committed state is missing.
        match platform::open_file(&path.join(NEXT), false, false) {
            Ok(_) => {
                fs::remove_file(path.join(NEXT)).map_err(|_| Error::Failed)?;
                directory.sync_all().map_err(|_| Error::Failed)?;
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(Error::Unavailable),
        }
        let store = Self {
            path: path.to_owned(),
            directory,
            lock,
            snapshot,
            digest: Sha256::digest(&bytes).into(),
            failed: false,
        };
        store.check_owner()?;
        Ok(store)
    }

    pub fn load<'a>(&self, key: u16, buffer: &'a mut [u8]) -> Result<Option<&'a [u8]>, Error> {
        if self.failed {
            return Err(Error::Failed);
        }
        let Some(value) = self.snapshot.entries.get(&key) else {
            return Ok(None);
        };
        if value.len() > buffer.len() {
            return Err(Error::BufferTooSmall);
        }
        buffer[..value.len()].copy_from_slice(value);
        Ok(Some(&buffer[..value.len()]))
    }

    pub fn store(&mut self, key: u16, value: &[u8]) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Failed);
        }
        if value.len() > MAX_VALUE_BYTES
            || (self.snapshot.entries.len() == MAX_ENTRIES
                && !self.snapshot.entries.contains_key(&key))
        {
            return Err(Error::Capacity);
        }
        let mut next = self.snapshot.clone();
        next.entries.insert(key, value.to_vec());
        self.commit(next)
    }

    pub fn remove(&mut self, key: u16) -> Result<(), Error> {
        if self.failed {
            return Err(Error::Failed);
        }
        let mut next = self.snapshot.clone();
        next.entries.remove(&key);
        self.commit(next)
    }

    fn check_owner(&self) -> Result<(), Error> {
        if !platform::same_file(&self.directory, &self.path).map_err(|_| Error::Changed)?
            || !platform::same_file(&self.lock, &self.path.join(LOCK))
                .map_err(|_| Error::Changed)?
        {
            return Err(Error::Changed);
        }
        // Recheck privacy and link admission on the currently named objects.
        platform::open_directory(&self.path).map_err(|_| Error::Changed)?;
        platform::open_file(&self.path.join(LOCK), false, false).map_err(|_| Error::Changed)?;
        Ok(())
    }

    fn commit(&mut self, mut next: Snapshot) -> Result<(), Error> {
        next.generation = next.generation.checked_add(1).ok_or(Error::Capacity)?;
        let bytes = next.encode()?;
        let result = (|| {
            self.check_owner()?;
            let previous = read_state(&self.path.join(STATE))?;
            if <[u8; 32]>::from(Sha256::digest(&previous)) != self.digest {
                return Err(Error::Changed);
            }
            let mut file = platform::open_file(&self.path.join(NEXT), true, true)
                .map_err(|_| Error::Failed)?;
            file.write_all(&bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| Error::Failed)?;
            self.check_owner()?;
            fs::rename(self.path.join(NEXT), self.path.join(STATE)).map_err(|_| Error::Failed)?;
            self.directory.sync_all().map_err(|_| Error::Failed)
        })();
        if result.is_err() {
            self.failed = true;
            return result;
        }
        self.snapshot = next;
        self.digest = Sha256::digest(&bytes).into();
        Ok(())
    }
}

fn validate_path(path: &Path) -> Result<(), Error> {
    if !path.is_absolute()
        || path.file_name().is_none()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(Error::InvalidPath);
    }
    Ok(())
}

#[cfg(test)]
mod tests;

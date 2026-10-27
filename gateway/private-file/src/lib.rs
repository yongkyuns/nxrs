//! Private regular files in trusted parent directories on a local Unix filesystem.
//!
//! Check the opened descriptor, never a pathname followed by a separate open.
//! This does not defend against changes by the service's own UID or a privileged
//! writer, hostile parent directories, filesystem stalls, ACLs or snapshot rollback.
#![forbid(unsafe_code)]
use std::{fs::File, path::Path};
#[cfg(unix)]
use std::{
    fs::OpenOptions,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Component,
};

type Result<T> = std::result::Result<T, &'static str>;

#[cfg(unix)]
fn path_valid(path: &Path) -> Result<()> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err("private file requires an absolute path without parent traversal");
    }
    Ok(())
}

#[cfg(unix)]
fn validate(file: &File, limit: u64, owner: Option<u32>) -> Result<()> {
    let metadata = file
        .metadata()
        .map_err(|_| "private file metadata unavailable")?;
    if !metadata.is_file() || metadata.mode() & 0o077 != 0 || metadata.nlink() != 1 {
        return Err("private file type, permissions or links invalid");
    }
    if owner.is_some_and(|owner| metadata.uid() != owner) {
        return Err("private file owner invalid");
    }
    if metadata.len() > limit {
        return Err("private file exceeds bound");
    }
    Ok(())
}

/// Reject symlinks, special files, shared permissions, hard links and oversized
/// input. O_NONBLOCK prevents a FIFO open from waiting for a writer before its
/// descriptor can be rejected; it is not a deadline for regular filesystem I/O.
#[cfg(unix)]
fn read_inner(path: &Path, limit: u64, owner: Option<u32>) -> Result<Vec<u8>> {
    path_valid(path)?;
    let bound = limit.checked_add(1).ok_or("invalid private file bound")?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "private file unavailable")?;
    validate(&file, limit, owner)?;
    let mut bytes = Vec::new();
    (&file)
        .take(bound)
        .read_to_end(&mut bytes)
        .map_err(|_| "private file read failed")?;
    if bytes.len() as u64 > limit {
        return Err("private file exceeds bound");
    }
    validate(&file, limit, owner)?;
    Ok(bytes)
}

/// Read a bounded private regular file in a trusted parent directory.
/// Reject final symlinks, special files, hard links and shared permissions.
/// O_NONBLOCK avoids a FIFO-open wait; it is not a regular-filesystem deadline.
/// This entry point imposes no expected owner beyond the trusted parent.
#[cfg(unix)]
pub fn read(path: &Path, limit: u64) -> Result<Vec<u8>> {
    read_inner(path, limit, None)
}

/// Require the expected owner on the actual opened descriptor, before and after
/// reading. This does not make an untrusted or replaceable parent directory safe.
#[cfg(unix)]
pub fn read_owned(path: &Path, limit: u64, owner: u32) -> Result<Vec<u8>> {
    read_inner(path, limit, Some(owner))
}

/// Create or open an empty private lock without truncating or following links.
/// Validate the same descriptor that holds the exclusive lock. The trusted
/// parent must not be replaced, and operators must never unlink an owned lock.
#[cfg(unix)]
pub fn lock(path: &Path) -> Result<File> {
    path_valid(path)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "state lock unavailable")?;
    validate(&file, 0, None)?;
    file.try_lock()
        .map_err(|_| "state already owned or lock unsupported")?;
    validate(&file, 0, None)?;
    Ok(file)
}

#[cfg(not(unix))]
pub fn read(_: &Path, _: u64) -> Result<Vec<u8>> {
    Err("private files require Unix")
}
#[cfg(not(unix))]
pub fn read_owned(_: &Path, _: u64, _: u32) -> Result<Vec<u8>> {
    Err("private files require Unix")
}
#[cfg(not(unix))]
pub fn lock(_: &Path) -> Result<File> {
    Err("private files require Unix")
}

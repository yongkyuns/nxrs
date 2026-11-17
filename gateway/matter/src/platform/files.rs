//! Shared Linux/macOS descriptor checks using std's Unix filesystem extensions.
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::Path;

pub(crate) fn create_directory(path: &Path) -> io::Result<()> {
    fs::DirBuilder::new().mode(0o700).create(path)?;
    File::open(path.parent().ok_or(io::ErrorKind::InvalidInput)?)?.sync_all()
}

pub(crate) fn open_directory(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY)
        .open(path)?;
    if file.metadata()?.mode() & 0o777 != 0o700 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(file)
}

/// Existing objects must be private regular files with one link. create_new
/// provides exclusive creation and never follows a pre-existing symlink.
pub(crate) fn open_file(path: &Path, create_new: bool, write: bool) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(write)
        .create_new(create_new)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.mode() & 0o777 != 0o600 || metadata.nlink() != 1 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(file)
}

pub(crate) fn same_file(opened: &File, path: &Path) -> io::Result<bool> {
    let a = opened.metadata()?;
    let b = fs::symlink_metadata(path)?;
    Ok(a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.mode() == b.mode()
        && a.uid() == b.uid()
        && a.nlink() == b.nlink())
}

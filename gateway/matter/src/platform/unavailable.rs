//! Portable state encoding remains available; private-file durability is unqualified here.
use std::{fs::File, io, path::Path};

pub(crate) fn create_directory(_: &Path) -> io::Result<()> {
    Err(io::ErrorKind::Unsupported.into())
}
pub(crate) fn open_directory(_: &Path) -> io::Result<File> {
    Err(io::ErrorKind::Unsupported.into())
}
pub(crate) fn open_file(_: &Path, _: bool, _: bool) -> io::Result<File> {
    Err(io::ErrorKind::Unsupported.into())
}
pub(crate) fn same_file(_: &File, _: &Path) -> io::Result<bool> {
    Err(io::ErrorKind::Unsupported.into())
}

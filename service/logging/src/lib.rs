//! Install a diagnostic backend and archive streams with bounded retention.
#![forbid(unsafe_code)]

mod facade;
mod file;

pub use facade::{install, Line};
pub use file::{start_file, FileArchive, FileLog};

use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};

/// Copy a blocking byte stream into `active` and retain one rotated file with
/// a `.1` suffix. Both files belong to one writer and are capped at `limit`.
/// Rotation preserves bytes and can split a line across the two files.
pub fn archive(mut source: impl Read, active: &Path, limit: NonZeroU64) -> io::Result<()> {
    let limit = limit.get();
    let mut name = active.as_os_str().to_owned();
    name.push(".1");
    let previous = PathBuf::from(name);
    let mut file = OpenOptions::new().create(true).append(true).open(active)?;
    let mut written = file.metadata()?.len();
    if written > limit {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "active log exceeds its limit",
        ));
    }

    let mut buffer = [0; 256];
    loop {
        let count = match source.read(&mut buffer) {
            Ok(0) => return file.flush(),
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let mut offset = 0;
        while offset < count {
            if written == limit {
                file.flush()?;
                drop(file);
                rotate(active, &previous)?;
                file = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(active)?;
                written = 0;
            }
            let size = ((count - offset) as u64).min(limit - written) as usize;
            file.write_all(&buffer[offset..offset + size])?;
            offset += size;
            written += size as u64;
        }
    }
}

fn rotate(active: &Path, previous: &Path) -> io::Result<()> {
    match fs::rename(active, previous) {
        // Some filesystems, including NuttX TMPFS, require an empty target name.
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            fs::remove_file(previous)?;
            fs::rename(active, previous)
        }
        result => result,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appends_across_reopen_and_keeps_only_the_latest_two_files() {
        let dir = std::env::temp_dir().join(format!("nxrs-log-router-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        let active = dir.join("system.log");
        let limit = NonZeroU64::new(4).unwrap();
        archive(&b"abc"[..], &active, limit).unwrap();
        archive(&b"defghijklmn"[..], &active, limit).unwrap();
        assert_eq!(fs::read(&active).unwrap(), b"mn");
        assert_eq!(fs::read(dir.join("system.log.1")).unwrap(), b"ijkl");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 2);
        fs::remove_dir_all(dir).unwrap();
    }
}

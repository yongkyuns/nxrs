//! Lifetime gate outside the replaceable activity tree. File ownership stays in Rust.
use super::Volume;
use crate::store::Error;
use std::{
    fs::{self, File},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    sync::Arc,
};

pub(crate) enum Access {
    Recording,
    Maintenance,
}
impl Volume {
    pub(crate) fn maintenance(&self) -> std::path::PathBuf {
        self.mount.join("maintenance")
    }
    pub(crate) fn lease(&self, access: Access) -> Result<Arc<File>, Error> {
        let directory = self.maintenance();
        let metadata = fs::symlink_metadata(&directory).map_err(|_| Error::Protection)?;
        if !metadata.is_dir()
            || metadata.mode() & 0o077 != 0
            || fs::canonicalize(&directory).map_err(|_| Error::Protection)? != directory
        {
            return Err(Error::Protection);
        }
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(directory.join("gate.lock"))?;
        let metadata = file.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.mode() & 0o077 != 0
            || metadata.len() != 0
        {
            return Err(Error::Protection);
        }
        match access {
            Access::Recording => {
                file.try_lock_shared().map_err(|_| Error::Busy)?;
                self.require_no_restore()?;
            }
            Access::Maintenance => file.try_lock().map_err(|_| Error::Busy)?,
        }
        Ok(Arc::new(file))
    }
    pub(crate) fn require_no_restore(&self) -> Result<(), Error> {
        match fs::symlink_metadata(self.maintenance().join("restore.json")) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            _ => Err(Error::Protection),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    #[test]
    fn recording_clones_retain_shared_ownership_and_restore_marker_blocks_startup() {
        const MODE: &str = "NXRS_TEST_ISOLATED_MAINTENANCE_LEASE";
        const PASSED: &[u8] = b"NXRS_MAINTENANCE_LIFETIME_WITNESS_PASS";
        if std::env::var_os(MODE).is_none() {
            // Concurrent fork/exec witnesses can transiently inherit an open
            // locked descriptor before exec closes it. This last-owner Drop
            // witness owns its descriptors in a separate, single-test process.
            // Production remains nonblocking and fails closed on Busy.
            use std::{
                io::Read,
                process::{Command, Stdio},
                time::{Duration, Instant},
            };
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "protection::lease::tests::recording_clones_retain_shared_ownership_and_restore_marker_blocks_startup", "--test-threads=1", "--nocapture"])
                .env(MODE, "1")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        assert!(
                            status.success(),
                            "isolated maintenance lifetime witness failed"
                        );
                        let mut output = Vec::new();
                        child
                            .stdout
                            .take()
                            .unwrap()
                            .take(8193)
                            .read_to_end(&mut output)
                            .unwrap();
                        assert!(output.len() <= 8192);
                        assert_eq!(output.windows(PASSED.len()).filter(|bytes| *bytes == PASSED).count(), 1,
                            "the selected witness must actually execute, never an empty test filter");
                        assert!(output
                            .windows(b"1 passed; 0 failed; 0 ignored;".len())
                            .any(|bytes| bytes == b"1 passed; 0 failed; 0 ignored;"));
                        return;
                    }
                    Ok(None) if Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(10))
                    }
                    _ => {
                        let _ = child.kill();
                        let _ = child.wait();
                        panic!("isolated maintenance lifetime witness did not retire");
                    }
                }
            }
        }
        assert_eq!(std::env::var(MODE).as_deref(), Ok("1"));
        let mut entropy = [0; 16];
        getrandom::fill(&mut entropy).unwrap();
        let id: String = entropy.iter().map(|v| format!("{v:02x}")).collect();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("nxrs-maintenance-{id}"));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let volume = Volume {
            layout_version: 2,
            mount: root.clone(),
            luks_uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
        };
        fs::DirBuilder::new()
            .mode(0o700)
            .create(volume.maintenance())
            .unwrap();
        let first = volume.lease(Access::Recording).unwrap();
        let clone = first.clone();
        let second = volume.lease(Access::Recording).unwrap();
        assert_eq!(volume.lease(Access::Maintenance).err(), Some(Error::Busy));
        drop(first);
        drop(second);
        assert_eq!(volume.lease(Access::Maintenance).err(), Some(Error::Busy));
        drop(clone);
        let exclusive = volume.lease(Access::Maintenance).unwrap();
        assert_eq!(volume.lease(Access::Recording).err(), Some(Error::Busy));
        fs::write(volume.maintenance().join("restore.json"), b"interrupted").unwrap();
        drop(exclusive);
        assert_eq!(
            volume.lease(Access::Recording).err(),
            Some(Error::Protection)
        );
        fs::remove_file(volume.maintenance().join("restore.json")).unwrap();
        let gate = volume.maintenance().join("gate.lock");
        fs::remove_file(&gate).unwrap();
        std::os::unix::fs::symlink("missing", &gate).unwrap();
        assert!(volume.lease(Access::Maintenance).is_err());
        fs::remove_file(&gate).unwrap();
        fs::write(&gate, b"unexpected").unwrap();
        fs::set_permissions(&gate, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            volume.lease(Access::Maintenance).err(),
            Some(Error::Protection)
        );
        fs::remove_dir_all(root).unwrap();
        println!("{}", std::str::from_utf8(PASSED).unwrap());
    }
}

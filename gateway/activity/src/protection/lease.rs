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
    }
}

//! Declared recording-volume protection. Ordinary development stores remain explicit.
use crate::store::Error;
use serde::Deserialize;
use std::path::{Component, Path, PathBuf};

#[cfg(target_os = "linux")]
#[path = "protection/linux.rs"]
mod platform;
#[cfg(not(target_os = "linux"))]
mod platform {
    pub(super) fn verify(_: &super::Volume) -> Result<(), crate::store::Error> {
        Err(crate::store::Error::Protection)
    }
}

/// Linux reference: one direct LUKS2 mapping, ext4, and the whole activity tree.
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Volume {
    pub mount: PathBuf,
    pub luks_uuid: String,
}
impl Volume {
    pub fn validate(&self, directory: &Path) -> Result<(), Error> {
        let mount = self.mount.to_str().ok_or(Error::Invalid)?;
        if !self.mount.is_absolute()
            || self.mount.components().count() < 3
            || self
                .mount
                .components()
                .any(|v| !matches!(v, Component::RootDir | Component::Normal(_)))
            || !mount
                .bytes()
                .all(|v| v.is_ascii_alphanumeric() || b"/._-".contains(&v))
            || mount.ends_with('/')
            || directory != self.mount.join("activity")
            || self.luks_uuid.len() != 36
            || !self.luks_uuid.bytes().enumerate().all(|(i, b)| {
                if [8, 13, 18, 23].contains(&i) {
                    b == b'-'
                } else {
                    b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
                }
            })
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
    pub fn verify(&self, directory: &Path) -> Result<(), Error> {
        self.validate(directory)?;
        platform::verify(self)
    }
}

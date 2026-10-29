use crate::{Error, Result};
use serde::Deserialize;
use std::{fs::OpenOptions, io::Read, net::SocketAddrV4, path::{Component, Path, PathBuf}};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub detections: Option<DetectionConfig>,
    pub camera: SocketAddrV4,
    /// Required acknowledgement: WireGuard on the PHONE does not encrypt this link.
    pub allow_insecure_camera_http: bool,
    #[serde(default)]
    pub camera_tls: Option<CameraTls>,
    pub rtsp: SocketAddrV4,
    pub stream_path: String,
    pub publisher_user: String,
    pub publisher_token_file: PathBuf,
    pub ffmpeg: PathBuf,
    pub publisher: PathBuf,
    pub width: u16,
    pub height: u16,
    pub frame_timeout_ms: u64,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DetectionConfig {
    pub control: SocketAddrV4,
    pub camera_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CameraTls {
    pub site_id: String,
    pub camera_id: String,
    pub gateway_id: String,
    pub issuer_sha256: String,
    pub enrollment_generation: u64,
    pub ca_certificate_file: PathBuf,
    pub camera_certificate_file: PathBuf,
    pub client_certificate_file: PathBuf,
    pub client_key_file: PathBuf,
    pub crl_file: PathBuf,
}
impl CameraTls {
    fn validate(&self) -> bool {
        identifier(&self.site_id) && identifier(&self.camera_id) && identifier(&self.gateway_id)
            && (1..=1_000_000_000).contains(&self.enrollment_generation)
            && self.issuer_sha256.len() == 64
            && self.issuer_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            && [&self.ca_certificate_file, &self.camera_certificate_file, &self.client_certificate_file,
                &self.client_key_file, &self.crl_file].iter().all(|p| absolute(p))
    }
}
fn identifier(s: &str) -> bool {
    (1..=64).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn absolute(path: &Path) -> bool {
    path.is_absolute() && !path.components().any(|p| matches!(p, Component::ParentDir))
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        if self.detections.as_ref().is_some_and(|v| {
            !v.control.ip().is_loopback()
                || v.control.port() == 0
                || !identifier(&v.camera_id)
                || v.control == self.rtsp
                || v.control == self.camera
        }) {
            return Err(Error::Configuration);
        }
        if self.allow_insecure_camera_http == self.camera_tls.is_some()
            || self.camera_tls.as_ref().is_some_and(|c| !c.validate())
            || self.detections.as_ref().zip(self.camera_tls.as_ref())
                .is_some_and(|(metadata, tls)| metadata.camera_id != tls.camera_id)
            || !(self.camera.ip().is_private() || self.camera.ip().is_loopback())
            || self.camera.port() == 0 || !self.rtsp.ip().is_loopback() || self.rtsp.port() == 0
            || self.camera == self.rtsp || !identifier(&self.stream_path) || !identifier(&self.publisher_user)
            || !absolute(&self.publisher_token_file) || !absolute(&self.ffmpeg) || !absolute(&self.publisher)
            || !(2..=1920).contains(&self.width) || !(2..=1080).contains(&self.height)
            || !self.width.is_multiple_of(2) || !self.height.is_multiple_of(2)
            || !(250..=10_000).contains(&self.frame_timeout_ms)
        { return Err(Error::Configuration); }
        Ok(())
    }
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_json::from_slice(&private_read(path, 8192)?).map_err(|_| Error::Configuration)?;
        config.validate()?;
        Ok(config)
    }
    pub fn token(&self) -> Result<String> {
        let bytes = private_read(&self.publisher_token_file, 8192)?;
        let value = std::str::from_utf8(&bytes).map_err(|_| Error::Configuration)?.trim_end_matches('\n');
        if value.len() != 64 || !value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(Error::Configuration);
        }
        Ok(value.to_owned())
    }
}
/// Trusted-parent-directory Unix profile. No symlink/hardlink, public file, or unbounded config.
#[cfg(unix)]
pub(crate) fn private_read(path: &Path, limit: u64) -> Result<Vec<u8>> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    if !absolute(path) { return Err(Error::Configuration); }
    let bound = limit.checked_add(1).ok_or(Error::Configuration)?;
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.mode() & 0o077 != 0 || metadata.nlink() != 1 || metadata.len() > limit {
        return Err(Error::Configuration);
    }
    let mut bytes = Vec::new();
    (&file).take(bound).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    if !after.is_file() || after.mode() & 0o077 != 0 || after.nlink() != 1 || after.len() > limit
        || bytes.len() as u64 > limit { return Err(Error::Configuration); }
    Ok(bytes)
}
#[cfg(not(unix))]
pub(crate) fn private_read(_: &Path, _: u64) -> Result<Vec<u8>> { Err(Error::Configuration) }

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_value(serde_json::json!({
            "camera":"192.168.1.2:8080", "allow_insecure_camera_http":true,
            "rtsp":"127.0.0.1:8554", "stream_path":"reference", "publisher_user":"publisher_reference",
            "publisher_token_file":"/private/publisher.token", "ffmpeg":"/usr/bin/ffmpeg", "publisher":"/usr/local/libexec/nxrs-rtsp-publisher",
            "width":640,"height":480,"frame_timeout_ms":3000
        })).unwrap()
    }
    #[test] fn valid_config_is_explicit_and_local() { assert!(config().validate().is_ok()); }
    #[test] fn rejects_public_source_and_nonlocal_publisher() {
        let mut c=config(); c.camera="8.8.8.8:80".parse().unwrap(); assert!(c.validate().is_err());
        let mut c=config(); c.rtsp="192.168.1.3:8554".parse().unwrap(); assert!(c.validate().is_err());
        let mut c=config(); c.allow_insecure_camera_http=false; assert!(c.validate().is_err());
    }
    #[test] fn rejects_paths_shell_fragments_and_invalid_bounds() {
        for bad in ["../other", "x;id", "x/y", "", "x\n"] {
            let mut c=config(); c.stream_path=bad.into(); assert!(c.validate().is_err());
        }
        let mut c=config(); c.ffmpeg="ffmpeg".into(); assert!(c.validate().is_err());
        let mut c=config(); c.publisher="publisher".into(); assert!(c.validate().is_err());
        let mut c=config(); c.width=641; assert!(c.validate().is_err());
        let mut c=config(); c.frame_timeout_ms=0; assert!(c.validate().is_err());
    }
}

use crate::{Error, Result};
use serde::Deserialize;
use std::{net::SocketAddrV4, path::{Component, Path, PathBuf}};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
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
    /// Required by the production service; omission is for explicit development use.
    #[serde(default)]
    pub media_sandbox: Option<PathBuf>,
    pub width: u16,
    pub height: u16,
    pub frame_timeout_ms: u64,
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
        if self.allow_insecure_camera_http == self.camera_tls.is_some()
            || self.camera_tls.as_ref().is_some_and(|c| !c.validate())
            || !(self.camera.ip().is_private() || self.camera.ip().is_loopback())
            || self.camera.port() == 0 || !self.rtsp.ip().is_loopback() || self.rtsp.port() == 0
            || self.camera == self.rtsp || !identifier(&self.stream_path) || !identifier(&self.publisher_user)
            || !absolute(&self.publisher_token_file) || !absolute(&self.ffmpeg) || !absolute(&self.publisher)
            || self.media_sandbox.as_ref().is_some_and(|p| !absolute(p))
            || !(2..=1920).contains(&self.width) || !(2..=1080).contains(&self.height)
            || !self.width.is_multiple_of(2) || !self.height.is_multiple_of(2)
            || !(250..=10_000).contains(&self.frame_timeout_ms)
        { return Err(Error::Configuration); }
        Ok(())
    }
    pub fn check_isolation(&self, config_path: &Path, required: bool) -> Result<()> {
        if required && self.media_sandbox.is_none() { return Err(Error::Configuration); }
        if self.media_sandbox.is_none() { return Ok(()); }
        let mut credentials = vec![config_path, self.publisher_token_file.as_path()];
        if let Some(tls) = &self.camera_tls {
            credentials.extend([tls.ca_certificate_file.as_path(), tls.camera_certificate_file.as_path(),
                tls.client_certificate_file.as_path(), tls.client_key_file.as_path(), tls.crl_file.as_path()]);
        }
        nxrs_media_sandbox::check_credentials(&self.ffmpeg, &credentials, None).map_err(|_| Error::Configuration)
    }
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_json::from_slice(&private_read(path)?).map_err(|_| Error::Configuration)?;
        config.validate()?;
        config.check_isolation(path, false)?;
        Ok(config)
    }
    pub fn token(&self) -> Result<String> {
        let bytes = private_read(&self.publisher_token_file)?;
        let value = std::str::from_utf8(&bytes).map_err(|_| Error::Configuration)?.trim_end_matches('\n');
        if value.len() != 64 || !value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(Error::Configuration);
        }
        Ok(value.to_owned())
    }
}
/// The same private-file policy as control security settings and denial state.
fn private_read(path: &Path) -> Result<Vec<u8>> {
    nxrs_gateway_private_file::read(path, 8192).map_err(|_| Error::Configuration)
}

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
    #[test] fn required_isolation_refuses_before_source_or_credential_io() {
        let c=config();
        assert_eq!(c.check_isolation(Path::new("/nonexistent/config"), true), Err(Error::Configuration));
        assert_eq!(c.check_isolation(Path::new("/nonexistent/config"), false), Ok(()));
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
        let mut c=config(); c.width=641; assert!(c.validate().is_err());
        let mut c=config(); c.frame_timeout_ms=0; assert!(c.validate().is_err());
    }
}

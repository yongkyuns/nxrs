use crate::{Error, Result};
use serde::Deserialize;
use std::{net::SocketAddrV4, path::{Component, Path, PathBuf}};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub camera: SocketAddrV4,
    /// Required acknowledgement: WireGuard on the PHONE does not encrypt this link.
    pub allow_insecure_camera_http: bool,
    pub rtsp: SocketAddrV4,
    pub stream_path: String,
    pub publisher_user: String,
    pub publisher_token_file: PathBuf,
    pub ffmpeg: PathBuf,
    pub width: u16,
    pub height: u16,
    pub frame_timeout_ms: u64,
}
fn identifier(s: &str) -> bool {
    (1..=64).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
fn absolute(path: &Path) -> bool {
    path.is_absolute() && !path.components().any(|p| matches!(p, Component::ParentDir))
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        if !self.allow_insecure_camera_http
            || !(self.camera.ip().is_private() || self.camera.ip().is_loopback())
            || self.camera.port() == 0 || !self.rtsp.ip().is_loopback() || self.rtsp.port() == 0
            || self.camera == self.rtsp || !identifier(&self.stream_path) || !identifier(&self.publisher_user)
            || !absolute(&self.publisher_token_file) || !absolute(&self.ffmpeg)
            || !(2..=1920).contains(&self.width) || !(2..=1080).contains(&self.height)
            || !self.width.is_multiple_of(2) || !self.height.is_multiple_of(2)
            || !(250..=10_000).contains(&self.frame_timeout_ms)
        { return Err(Error::Configuration); }
        Ok(())
    }
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = serde_json::from_slice(&private_read(path)?).map_err(|_| Error::Configuration)?;
        config.validate()?;
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
            "publisher_token_file":"/private/publisher.token", "ffmpeg":"/usr/bin/ffmpeg",
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
        let mut c=config(); c.width=641; assert!(c.validate().is_err());
        let mut c=config(); c.frame_timeout_ms=0; assert!(c.validate().is_err());
    }
}

//! Static enrollment for the single-host development gateway. No firmware dependencies.
use axum::http::Uri;
use serde::Deserialize;
use std::{collections::HashSet, net::SocketAddr};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub auth_listen: SocketAddr,
    pub grant_ttl_seconds: u64,
    #[serde(default)]
    pub credential_window: Option<crate::credentials::Window>,
    pub max_grants: usize,
    pub media_origins: Vec<String>,
    pub cameras: Vec<Camera>,
    pub viewers: Vec<Viewer>,
    pub publishers: Vec<Publisher>,
    #[serde(default)]
    pub recorders: Vec<Recorder>,
    #[serde(default)]
    pub activity: Option<nxrs_camera_activity::store::Settings>,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Camera {
    pub camera_id: String,
    pub gateway_id: String,
    pub site_id: String,
    pub name: String,
    pub source: Source,
    pub stream_path: String,
    pub whep_endpoint: String,
}
#[derive(Clone, Copy, Deserialize, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Source { Synthetic, Physical }
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Viewer {
    pub viewer_id: String,
    pub site_id: String,
    pub camera_ids: Vec<String>,
    /// SHA-256 of a generated 256-bit hex token, NOT a human password.
    pub token_sha256: String,
    #[serde(default)]
    pub manage_recordings: bool,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Publisher {
    pub username: String,
    pub camera_id: String,
    pub token_sha256: String,
}

/// Separate service identity: only read the named camera over loopback RTSP.
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Recorder {
    pub username: String,
    pub camera_id: String,
    pub token_sha256: String,
}

pub fn identifier(s: &str) -> bool {
    !s.is_empty() && s.len() <= 64 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
pub fn hex_token(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn origin(s: &str) -> Option<String> {
    let uri: Uri = s.parse().ok()?;
    let auth = uri.authority()?;
    if uri.scheme_str() != Some("https") || uri.query().is_some()
        || s.contains(['@', '#', '%', '\\']) || !s.is_ascii()
        || auth.host().is_empty() || auth.port_u16() == Some(0)
        || uri.path().split('/').any(|p| p == "." || p == "..") {
        return None;
    }
    Some(format!("{}:{}", auth.host().to_ascii_lowercase(), auth.port_u16().unwrap_or(443)))
}
impl Config {
    /// Errors are intentionally non-diagnostic about input values: never echo credentials/config.
    pub fn validate(&self) -> Result<(), &'static str> {
        if !self.listen.ip().is_loopback() || !self.auth_listen.ip().is_loopback()
            || self.listen == self.auth_listen || self.listen.port() == 0 || self.auth_listen.port() == 0 {
            return Err("distinct nonzero loopback listeners required");
        }
        if !(1..=300).contains(&self.grant_ttl_seconds) || !(1..=1024).contains(&self.max_grants)
            || !(1..=64).contains(&self.cameras.len()) || !(1..=64).contains(&self.viewers.len())
            || self.publishers.len() > 64 || self.recorders.len() > 64 || !(1..=8).contains(&self.media_origins.len()) {
            return Err("configuration bounds exceeded");
        }
        if let Some(window) = self.credential_window { window.validate()?; }
        let mut origins = HashSet::new();
        for s in &self.media_origins {
            let uri: Uri = s.parse().map_err(|_| "invalid media origin")?;
            if uri.path() != "/" && !uri.path().is_empty() { return Err("origin must not include a path"); }
            origins.insert(origin(s).ok_or("invalid media origin")?);
        }
        let mut ids = HashSet::new();
        let mut paths = HashSet::new();
        let mut endpoints = HashSet::new();
        for c in &self.cameras {
            if !identifier(&c.camera_id) || !identifier(&c.gateway_id) || !identifier(&c.site_id)
                || !identifier(&c.stream_path) || c.name.is_empty() || c.name.chars().count() > 128
                || c.name.chars().any(char::is_control) || !ids.insert(&c.camera_id)
                || !paths.insert(&c.stream_path) || !endpoints.insert(&c.whep_endpoint)
                || !origins.contains(&origin(&c.whep_endpoint).ok_or("invalid media endpoint")?) {
                return Err("invalid or ambiguous camera");
            }
            let uri: Uri = c.whep_endpoint.parse().map_err(|_| "invalid media endpoint")?;
            // Direct MediaMTX mapping for this single-host profile; never trust a client path.
            if uri.path() != format!("/{}/whep", c.stream_path) { return Err("endpoint/path mismatch"); }
        }
        let mut principals = HashSet::new();
        let mut credentials = HashSet::new();
        for v in &self.viewers {
            if !identifier(&v.viewer_id) || !identifier(&v.site_id) || !principals.insert(&v.viewer_id)
                || !hex_token(&v.token_sha256) || !credentials.insert(&v.token_sha256)
                || v.camera_ids.is_empty() || v.camera_ids.len() > 64 {
                return Err("invalid viewer");
            }
            let mut visible = HashSet::new();
            for id in &v.camera_ids {
                if !visible.insert(id) || !self.cameras.iter().any(|c| &c.camera_id == id && c.site_id == v.site_id) {
                    return Err("invalid viewer camera scope");
                }
            }
        }
        for p in &self.publishers {
            if !identifier(&p.username) || !principals.insert(&p.username) || !hex_token(&p.token_sha256)
                || !credentials.insert(&p.token_sha256) || !self.cameras.iter().any(|c| c.camera_id == p.camera_id) {
                return Err("invalid publisher or reused credential");
            }
        }
        if let Some(settings) = &self.activity {
            settings.validate().map_err(|_| "invalid activity storage")?;
            if settings.camera_ids.iter().any(|id| !self.cameras.iter().any(|c| &c.camera_id == id)) {
                return Err("unknown activity camera");
            }
        }
        let mut recorded = HashSet::new();
        for r in &self.recorders {
            if !identifier(&r.username) || !principals.insert(&r.username) || !hex_token(&r.token_sha256)
                || !credentials.insert(&r.token_sha256) || !recorded.insert(&r.camera_id)
                || !self.activity.as_ref().is_some_and(|a| a.camera_ids.contains(&r.camera_id)) {
                return Err("invalid recorder or reused credential");
            }
        }
        Ok(())
    }
}

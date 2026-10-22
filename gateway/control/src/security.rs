//! Optional durable identity denial and bounded WebRTC ownership, without media proxying.
use crate::{config::{self, Config}, policy::{MediaAuth, Policy}, revocations::{private_read, Store}};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::{HashMap, HashSet}, net::SocketAddr, path::{Path, PathBuf}, time::{Duration, Instant}};
use subtle::ConstantTimeEq;

pub const MAX_SESSIONS: usize = 256;
const MAX_PER_VIEWER: usize = 4;
const HEALTH_WINDOW: Duration = Duration::from_secs(5);
const NEGOTIATION_GRACE: Duration = Duration::from_secs(30);

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub revocations_file: PathBuf,
    pub media_api: SocketAddr,
    pub media_api_token: String,
}
impl Settings {
    pub fn read(path: &Path) -> Result<Self, &'static str> {
        serde_json::from_slice(&private_read(path, 16_384)?).map_err(|_| "invalid security settings")
    }
    fn validate(&self, config: &Config) -> Result<(), &'static str> {
        let token_hash = hash_hex(&self.media_api_token);
        if !self.media_api.ip().is_loopback() || self.media_api.port() == 0
            || self.media_api == config.listen || self.media_api == config.auth_listen
            || !config::hex_token(&self.media_api_token) || !self.revocations_file.is_absolute()
            || config.viewers.iter().any(|v| v.token_sha256 == token_hash)
            || config.publishers.iter().any(|p| p.token_sha256 == token_hash) {
            return Err("invalid or reused media-management credentials/address");
        }
        Ok(())
    }
}
fn hash_hex(token: &str) -> String {
    Sha256::digest(token.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}
pub fn session_id(id: &str) -> bool {
    id.len() == 36 && id.bytes().enumerate().all(|(i, b)| {
        if [8, 13, 18, 23].contains(&i) { b == b'-' }
        else { b.is_ascii_digit() || (b'a'..=b'f').contains(&b) }
    })
}

#[derive(Deserialize)]
pub struct Auth {
    #[serde(flatten)] pub request: MediaAuth,
    #[serde(default)] pub id: Option<String>,
}
#[derive(Deserialize)]
pub struct RemoteSession {
    pub id: String,
    pub state: String,
    pub path: String,
    #[serde(rename = "peerConnectionEstablished")]
    pub established: bool,
}
struct Permit { viewer: String, expires: u64, deadline: Instant }
struct Session {
    viewer: String,
    path: String,
    admitted: Instant,
    observed: bool,
    invalid_mapping: bool,
}
struct Enforcement {
    settings: Settings,
    store: Store,
    storage_failed: bool,
    permits: HashMap<String, Permit>,
    sessions: HashMap<String, Session>,
    last_sync: Option<Instant>,
    bootstrapped: bool,
}
impl Enforcement {
    fn ready(&self, now: Instant) -> bool {
        !self.storage_failed && self.last_sync.is_some_and(|at| now.saturating_duration_since(at) <= HEALTH_WINDOW)
    }
    fn prune(&mut self, wall: u64, now: Instant) {
        self.permits.retain(|_, permit| wall < permit.expires && now < permit.deadline);
    }
}

pub struct GatewayPolicy {
    inner: Policy,
    config: Config,
    enforcement: Option<Enforcement>,
    pub(crate) detections: crate::detections::Store,
}
impl GatewayPolicy {
    pub fn new(config: Config, settings: Option<Settings>) -> Result<Self, &'static str> {
        let mut inner = Policy::new(config.clone())?;
        let enforcement = if let Some(settings) = settings {
            settings.validate(&config)?;
            let store = Store::open(&settings.revocations_file)?;
            for id in &store.revoked {
                // Preserve old IDs in the store even if they have been removed from enrollment.
                if config.viewers.iter().any(|v| &v.viewer_id == id) {
                    inner.revoke(id).map_err(|_| "cannot restore revocation")?;
                }
            }
            Some(Enforcement { settings, store, storage_failed: false, permits: HashMap::new(),
                sessions: HashMap::new(), last_sync: None, bootstrapped: false })
        } else { None };
        let detections = crate::detections::Store::new(&config);
        Ok(Self { inner, config, enforcement, detections })
    }
    pub fn catalog(&self, token: &str) -> Result<Value, StatusCode> {
        let catalog = self.inner.catalog(token)?;
        if self.enforcement.as_ref().is_some_and(|e| e.storage_failed) {
            return Err(StatusCode::SERVICE_UNAVAILABLE);
        }
        Ok(catalog)
    }
    pub(crate) fn detection_viewer(&self, token: &str, camera: &str) -> Result<String, StatusCode> {
        let viewer = self.inner.camera_viewer(token, camera)?;
        if self.enforcement.as_ref().is_some_and(|e| e.storage_failed) {
            return Err(StatusCode::SERVICE_UNAVAILABLE);
        }
        Ok(viewer)
    }
    pub(crate) fn detection_publisher(&self, token: &str, user: &str, camera: &str) -> bool {
        self.inner.publisher(token, user, camera)
    }
    pub fn issue(&mut self, token: &str, camera: &str, wall: u64, now: Instant) -> Result<Value, StatusCode> {
        if let Some(e) = &mut self.enforcement {
            self.inner.catalog(token)?;
            e.prune(wall, now);
            if !e.ready(now) { return Err(StatusCode::SERVICE_UNAVAILABLE); }
            if e.permits.len() >= self.config.max_grants { return Err(StatusCode::TOO_MANY_REQUESTS); }
        }
        let grant = self.inner.issue(token, camera, wall, now)?;
        if let Some(e) = &mut self.enforcement {
            let token_hash = hash_hex(token);
            let viewer = self.config.viewers.iter().find(|v|
                bool::from(v.token_sha256.as_bytes().ct_eq(token_hash.as_bytes())))
                .ok_or(StatusCode::UNAUTHORIZED)?;
            let media_token = grant["access_token"].as_str().ok_or(StatusCode::SERVICE_UNAVAILABLE)?;
            e.permits.insert(hash_hex(media_token), Permit { viewer: viewer.viewer_id.clone(),
                expires: grant["expires_at"].as_u64().ok_or(StatusCode::SERVICE_UNAVAILABLE)?,
                deadline: now + Duration::from_secs(self.config.grant_ttl_seconds) });
        }
        Ok(grant)
    }
    pub fn authorize_media(&mut self, auth: &Auth, wall: u64, now: Instant) -> bool {
        let request = &auth.request;
        if let Some(e) = &self.enforcement {
            if request.action == "api" {
                // MediaMTX API permissions are coarse. Never send this privileged token to viewers.
                return request.user.is_empty() && request.password.is_empty() && request.path.is_empty()
                    && request.protocol.is_empty() && ["", "itemsPerPage=256"].contains(&request.query.as_str())
                    && config::hex_token(&request.token)
                    && bool::from(request.token.as_bytes().ct_eq(e.settings.media_api_token.as_bytes()));
            }
        }
        if !self.inner.authorize_media(request, wall, now) { return false; }
        if request.action != "read" { return true; }
        let Some(e) = &mut self.enforcement else { return true; };
        e.prune(wall, now);
        if !e.ready(now) { return false; }
        let Some(id) = auth.id.as_deref().filter(|id| session_id(id)) else { return false; };
        let Some(permit) = e.permits.get(&hash_hex(&request.token)) else { return false; };
        if let Some(existing) = e.sessions.get(id) {
            return !existing.invalid_mapping && existing.viewer == permit.viewer && existing.path == request.path;
        }
        if e.sessions.len() >= MAX_SESSIONS
            || e.sessions.values().filter(|s| s.viewer == permit.viewer).count() >= MAX_PER_VIEWER { return false; }
        e.sessions.insert(id.to_owned(), Session { viewer: permit.viewer.clone(), path: request.path.clone(),
            admitted: now, observed: false, invalid_mapping: false });
        true
    }
    /// The HTTP handler executes this on a blocking worker; cancellation cannot undo a denial.
    pub fn revoke(&mut self, viewer: &str) -> Result<StatusCode, StatusCode> {
        self.inner.revoke(viewer)?;
        let Some(e) = &mut self.enforcement else { return Ok(StatusCode::NO_CONTENT); };
        e.permits.retain(|_, permit| permit.viewer != viewer);
        match e.store.revoke(viewer) {
            Ok(()) => { e.storage_failed = false; Ok(StatusCode::ACCEPTED) }
            Err(_) => { e.storage_failed = true; Err(StatusCode::SERVICE_UNAVAILABLE) }
        }
    }
    pub fn revocation_status(&self, viewer: &str, now: Instant) -> Result<Value, StatusCode> {
        if !self.config.viewers.iter().any(|v| v.viewer_id == viewer) { return Err(StatusCode::NOT_FOUND); }
        let Some(e) = &self.enforcement else { return Err(StatusCode::NOT_FOUND); };
        Ok(json!({"revoked":e.store.revoked.contains(viewer), "persistence_healthy":!e.storage_failed,
            "media_control_ready":e.ready(now),
            "tracked_sessions":e.sessions.values().filter(|s| s.viewer == viewer).count()}))
    }
    pub fn plan(&mut self, remote: &[RemoteSession], now: Instant) -> Vec<String> {
        let Some(e) = &mut self.enforcement else { return Vec::new(); };
        let present: HashSet<_> = remote.iter().map(|s| s.id.as_str()).collect();
        // Unobserved admissions get a bounded negotiation grace; observed sessions are freed on
        // their first absent snapshot. A just-authorized session cannot be removed by an older list.
        e.sessions.retain(|id, s| present.contains(id.as_str())
            || (!s.observed && now.saturating_duration_since(s.admitted) < NEGOTIATION_GRACE));
        for remote in remote {
            if let Some(owned) = e.sessions.get_mut(&remote.id) {
                owned.observed = true;
                if remote.state != "read" || owned.path != remote.path { owned.invalid_mapping = true; }
            }
        }
        remote.iter().filter(|s| s.state == "read" && match e.sessions.get(&s.id) {
            Some(owned) => e.storage_failed || e.store.revoked.contains(&owned.viewer) || owned.invalid_mapping,
            None => !e.bootstrapped || s.established,
        }).map(|s| s.id.clone()).collect()
    }
    pub fn still_forbidden(&self, id: &str) -> bool {
        let Some(e) = &self.enforcement else { return false; };
        match e.sessions.get(id) {
            Some(s) => e.storage_failed || e.store.revoked.contains(&s.viewer) || s.invalid_mapping,
            None => true,
        }
    }
    pub fn retired(&mut self, id: &str) {
        if let Some(e) = &mut self.enforcement { e.sessions.remove(id); }
    }
    pub fn synchronized(&mut self, success: bool, now: Instant) {
        if let Some(e) = &mut self.enforcement {
            e.last_sync = success.then_some(now);
            if success { e.bootstrapped = true; }
        }
    }
}

//! Application authorization, deliberately independent of VPN peers and IP addresses.
use crate::config::{hex_token, Config, Viewer};
use axum::http::StatusCode;
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

pub type Result<T> = std::result::Result<T, StatusCode>;

fn hash(s: &str) -> [u8; 32] {
    Sha256::digest(s.as_bytes()).into()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn matches(token: &str, expected: &str) -> bool {
    hex_token(token) && bool::from(hex(&hash(token)).as_bytes().ct_eq(expected.as_bytes()))
}

struct Grant {
    viewer_id: String,
    camera_id: String,
    expires_at: u64,
    deadline: Instant,
}

pub struct Policy {
    config: Config,
    grants: HashMap<[u8; 32], Grant>,
    disabled_viewers: HashSet<String>,
}

/// Reachable only on the separate loopback listener. Ignore upstream metadata fields.
#[derive(Deserialize)]
pub struct MediaAuth {
    #[serde(default)]
    pub user: String,
    #[serde(default)]
    pub password: String,
    #[serde(default)]
    pub token: String,
    pub action: String,
    pub path: String,
    pub protocol: String,
    #[serde(default)]
    pub query: String,
}

impl Policy {
    pub fn new(config: Config) -> std::result::Result<Self, &'static str> {
        config.validate()?;
        Ok(Self {
            config,
            grants: HashMap::new(),
            disabled_viewers: HashSet::new(),
        })
    }

    fn viewer(&self, token: &str) -> Result<&Viewer> {
        // Fixed-size digests are compared in constant time. Tokens are generated, not passwords.
        let viewer = self.config.viewers.iter()
            .find(|v| matches(token, &v.token_sha256))
            .ok_or(StatusCode::UNAUTHORIZED)?;
        if self.disabled_viewers.contains(&viewer.viewer_id) {
            return Err(StatusCode::FORBIDDEN);
        }
        Ok(viewer)
    }

    pub fn activity_scope(&self, token: &str, camera: &str, manage: bool) -> Result<bool> {
        let viewer = self.viewer(token)?;
        if !self.config.cameras.iter().any(|c| c.camera_id == camera && c.site_id == viewer.site_id)
            || !viewer.camera_ids.iter().any(|id| id == camera) { return Err(StatusCode::NOT_FOUND); }
        if manage && !viewer.manage_recordings { return Err(StatusCode::FORBIDDEN); }
        Ok(viewer.manage_recordings)
    }

    pub fn catalog(&self, token: &str) -> Result<Value> {
        let viewer = self.viewer(token)?;
        let cameras: Vec<Value> = self.config.cameras.iter()
            .filter(|c| c.site_id == viewer.site_id && viewer.camera_ids.contains(&c.camera_id))
            .map(|c| json!({
                "camera_id": c.camera_id, "gateway_id": c.gateway_id, "site_id": c.site_id,
                "name": c.name, "source": c.source, "availability": "unknown", "live_video": true
            }))
            .collect();
        // Configured capability is not an observed online publisher or fresh frame.
        Ok(json!({"api_version": "1", "cameras": cameras}))
    }

    pub fn camera_viewer(&self, token: &str, camera: &str) -> Result<String> {
        let viewer = self.viewer(token)?;
        if !self.config.cameras.iter().any(|c| {
            c.camera_id == camera
                && c.site_id == viewer.site_id
                && viewer.camera_ids.contains(&c.camera_id)
        }) {
            return Err(StatusCode::NOT_FOUND);
        }
        Ok(viewer.viewer_id.clone())
    }
    pub fn publisher(&self, token: &str, user: &str, camera: &str) -> bool {
        self.config
            .publishers
            .iter()
            .any(|p| p.username == user && p.camera_id == camera && matches(token, &p.token_sha256))
    }


    fn prune(&mut self, wall: u64, now: Instant) {
        self.grants.retain(|_, grant| wall < grant.expires_at && now < grant.deadline);
    }

    pub fn issue(&mut self, token: &str, camera: &str, wall: u64, now: Instant) -> Result<Value> {
        let viewer = self.viewer(token)?.clone();
        let camera = self.config.cameras.iter()
            .find(|c| c.camera_id == camera && c.site_id == viewer.site_id
                && viewer.camera_ids.contains(&c.camera_id))
            .cloned()
            .ok_or(StatusCode::NOT_FOUND)?;
        self.prune(wall, now);
        if self.grants.len() >= self.config.max_grants
            || self.grants.values().filter(|g| g.viewer_id == viewer.viewer_id).count() >= 16
        {
            return Err(StatusCode::TOO_MANY_REQUESTS);
        }
        let mut entropy = [0u8; 32];
        getrandom::fill(&mut entropy).map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
        let access_token = hex(&entropy);
        let digest = hash(&access_token);
        if self.grants.contains_key(&digest) {
            return Err(StatusCode::SERVICE_UNAVAILABLE);
        }
        let expires_at = wall.checked_add(self.config.grant_ttl_seconds)
            .ok_or(StatusCode::SERVICE_UNAVAILABLE)?;
        self.grants.insert(digest, Grant {
            viewer_id: viewer.viewer_id,
            camera_id: camera.camera_id.clone(),
            expires_at,
            deadline: now + Duration::from_secs(self.config.grant_ttl_seconds),
        });
        Ok(json!({
            "api_version": "1", "camera_id": camera.camera_id, "protocol": "whep",
            "endpoint": camera.whep_endpoint, "access_token": access_token,
            "token_type": "Bearer", "expires_at": expires_at
        }))
    }

    pub fn authorize_media(&mut self, auth: &MediaAuth, wall: u64, now: Instant) -> bool {
        self.prune(wall, now);
        if !auth.query.is_empty() {
            return false;
        }
        if auth.action == "read" && auth.protocol == "webrtc"
            && auth.user.is_empty() && auth.password.is_empty() && hex_token(&auth.token)
        {
            return self.grants.get(&hash(&auth.token)).is_some_and(|grant| {
                !self.disabled_viewers.contains(&grant.viewer_id)
                    && self.config.cameras.iter().any(|camera|
                        camera.camera_id == grant.camera_id && camera.stream_path == auth.path)
            });
        }
        // MediaMTX v1.21.2 internal/auth/manager.go:getToken mirrors a nonempty
        // RTSP password into token. These must represent the SAME credential;
        // a different bearer token must never supplement or bypass the publisher password.
        if auth.action == "publish" && auth.protocol == "rtsp"
            && auth.token == auth.password
        {
            return self.config.publishers.iter().any(|publisher| {
                publisher.username == auth.user
                    && matches(&auth.password, &publisher.token_sha256)
                    && self.config.cameras.iter().any(|camera|
                        camera.camera_id == publisher.camera_id && camera.stream_path == auth.path)
            });
        }
        if auth.action == "read" && auth.protocol == "rtsp" && auth.token == auth.password {
            return self.config.recorders.iter().any(|r| r.username == auth.user
                && matches(&auth.password, &r.token_sha256)
                && self.config.cameras.iter().any(|c| c.camera_id == r.camera_id && c.stream_path == auth.path));
        }
        false
    }

    /// Invalidates admission and outstanding grants, NOT already-established WebRTC sessions.
    /// Persistence/active termination are explicit later production gates.
    pub fn revoke(&mut self, viewer: &str) -> Result<()> {
        if !self.config.viewers.iter().any(|v| v.viewer_id == viewer) {
            return Err(StatusCode::NOT_FOUND);
        }
        self.disabled_viewers.insert(viewer.to_owned());
        self.grants.retain(|_, grant| grant.viewer_id != viewer);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Config {
        serde_json::from_value(json!({
            "listen": "127.0.0.1:18080", "auth_listen": "127.0.0.1:18081",
            "grant_ttl_seconds": 60, "max_grants": 2, "media_origins": ["https://media.test"],
            "cameras": [{
                "camera_id": "cam_one", "gateway_id": "gw_one", "site_id": "site_one",
                "name": "Synthetic", "source": "synthetic", "stream_path": "one",
                "whep_endpoint": "https://media.test/one/whep"
            }],
            "viewers": [{
                "viewer_id": "viewer_one", "site_id": "site_one", "camera_ids": ["cam_one"],
                "token_sha256": hex(&hash(&"a".repeat(64)))
            }],
            "publishers": [{
                "username": "publisher_one", "camera_id": "cam_one",
                "token_sha256": hex(&hash(&"b".repeat(64)))
            }]
        })).unwrap()
    }

    fn media(token: &str) -> MediaAuth {
        MediaAuth {
            user: String::new(), password: String::new(), token: token.into(),
            action: "read".into(), path: "one".into(), protocol: "webrtc".into(), query: String::new(),
        }
    }

    #[test]
    fn grant_scope_and_role_separation() {
        let mut policy = Policy::new(config()).unwrap();
        let now = Instant::now();
        assert!(policy.catalog(&"b".repeat(64)).is_err());
        assert_eq!(policy.issue(&"a".repeat(64), "other", 1000, now).unwrap_err(), StatusCode::NOT_FOUND);
        let grant = policy.issue(&"a".repeat(64), "cam_one", 1000, now).unwrap();
        let token = grant["access_token"].as_str().unwrap();
        assert!(policy.authorize_media(&media(token), 1001, now));
        assert!(!policy.authorize_media(&media(&"a".repeat(64)), 1001, now));
        for action in ["publish", "api", "metrics", "pprof", "playback"] {
            let mut auth = media(token);
            auth.action = action.into();
            assert!(!policy.authorize_media(&auth, 1001, now));
        }
        let mut auth = media(token);
        auth.path = "other".into();
        assert!(!policy.authorize_media(&auth, 1001, now));
        let mut auth = media(token);
        auth.protocol = "rtsp".into();
        assert!(!policy.authorize_media(&auth, 1001, now));
        let mut auth = media(token);
        auth.query = "token=redacted".into();
        assert!(!policy.authorize_media(&auth, 1001, now));
    }

    #[test]
    fn expiry_obeys_monotonic_and_wall_clocks() {
        for (wall, elapsed) in [(1060, 0), (999, 60)] {
            let mut policy = Policy::new(config()).unwrap();
            let now = Instant::now();
            let grant = policy.issue(&"a".repeat(64), "cam_one", 1000, now).unwrap();
            assert!(!policy.authorize_media(&media(grant["access_token"].as_str().unwrap()),
                wall, now + Duration::from_secs(elapsed)));
        }
    }

    #[test]
    fn capacity_expiry_and_revocation() {
        let mut policy = Policy::new(config()).unwrap();
        let now = Instant::now();
        let grant = policy.issue(&"a".repeat(64), "cam_one", 1000, now).unwrap();
        policy.issue(&"a".repeat(64), "cam_one", 1000, now).unwrap();
        assert_eq!(policy.issue(&"a".repeat(64), "cam_one", 1000, now).unwrap_err(), StatusCode::TOO_MANY_REQUESTS);
        policy.issue(&"a".repeat(64), "cam_one", 1061, now + Duration::from_secs(61)).unwrap();
        policy.revoke("viewer_one").unwrap();
        assert!(!policy.authorize_media(&media(grant["access_token"].as_str().unwrap()), 1001, now));
        assert_eq!(policy.catalog(&"a".repeat(64)).unwrap_err(), StatusCode::FORBIDDEN);
        assert!(policy.grants.is_empty());
    }

    #[test]
    fn publisher_only_publishes_exact_path() {
        let mut policy = Policy::new(config()).unwrap();
        let now = Instant::now();
        let mut auth = media(&"b".repeat(64));
        auth.action = "publish".into();
        auth.protocol = "rtsp".into();
        auth.user = "publisher_one".into();
        auth.password = "b".repeat(64);
        assert!(policy.authorize_media(&auth, 1000, now));
        auth.action = "read".into();
        assert!(!policy.authorize_media(&auth, 1000, now));
        auth.action = "publish".into();
        auth.path = "other".into();
        assert!(!policy.authorize_media(&auth, 1000, now));
    }

    #[test]
    fn mediamtx_password_mirror_does_not_allow_mixed_credentials() {
        let mut policy = Policy::new(config()).unwrap();
        let now = Instant::now();
        let mut auth = media("");
        auth.action = "publish".into();
        auth.protocol = "rtsp".into();
        auth.user = "publisher_one".into();
        auth.password = "b".repeat(64);
        assert!(!policy.authorize_media(&auth, 1000, now));
        auth.token = "a".repeat(64);
        assert!(!policy.authorize_media(&auth, 1000, now));
        auth.token = auth.password.clone();
        assert!(policy.authorize_media(&auth, 1000, now));
        auth.password = "a".repeat(64);
        auth.token = auth.password.clone();
        assert!(!policy.authorize_media(&auth, 1000, now));
        auth.password.clear();
        auth.token = "b".repeat(64);
        assert!(!policy.authorize_media(&auth, 1000, now));
    }

    #[test]
    fn rejects_unsafe_configurations() {
        let config = config();
        assert!(config.validate().is_ok());
        let mut invalid = config.clone();
        invalid.listen = "0.0.0.0:8080".parse().unwrap();
        assert!(invalid.validate().is_err());
        let mut invalid = config.clone();
        invalid.viewers[0].site_id = "other_site".into();
        assert!(invalid.validate().is_err());
        let mut invalid = config.clone();
        invalid.publishers[0].token_sha256 = invalid.viewers[0].token_sha256.clone();
        assert!(invalid.validate().is_err());
        let mut invalid = config.clone();
        invalid.cameras.push(invalid.cameras[0].clone());
        assert!(invalid.validate().is_err());
        for endpoint in [
            "http://media.test/one/whep", "https://evil.test/one/whep",
            "https://media.test/one/whep?token=x", "https://user@media.test/one/whep",
            "https://media.test/../one/whep", "https://media.test/other/whep",
        ] {
            let mut invalid = config.clone();
            invalid.cameras[0].whep_endpoint = endpoint.into();
            assert!(invalid.validate().is_err());
        }
    }
}

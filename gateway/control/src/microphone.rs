//! A short capture lease owned by the camera's authorized privacy controller.
use crate::{bearer, publisher_identity, Failure, Shared};
use axum::{
    extract::{rejection::JsonRejection, Path, State},
    http::{HeaderMap, StatusCode},
    Json,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

const LEASE: Duration = Duration::from_millis(1500);

#[derive(Debug, Eq, PartialEq, Serialize)]
pub(crate) struct Permit {
    id: [u8; 16],
    valid_for_ms: u64,
}

#[derive(Default)]
pub(crate) struct Leases(HashMap<String, ([u8; 16], Instant)>);

impl Leases {
    pub(crate) fn clear(&mut self, camera: &str) {
        self.0.remove(camera);
    }

    fn renew(&mut self, camera: String, id: [u8; 16], now: Instant) {
        self.0.insert(camera, (id, now + LEASE));
    }

    fn active(&self, camera: &str, now: Instant) -> Option<Permit> {
        let (id, end) = self.0.get(camera)?;
        let valid_for_ms = end.saturating_duration_since(now).as_millis() as u64;
        (valid_for_ms > 0).then_some(Permit {
            id: *id,
            valid_for_ms,
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Change {
    permit: Option<[u8; 16]>,
}

pub(crate) async fn write(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    payload: Result<Json<Change>, JsonRejection>,
) -> Result<StatusCode, Failure> {
    let mut p = shared.lock().await;
    p.microphone_controller(bearer(&headers)?, &camera)
        .map_err(Failure)?;
    let Json(change) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if let Some(permit) = change.permit {
        p.microphone.renew(camera, permit, Instant::now());
    } else {
        p.microphone.clear(&camera);
    }
    Ok(StatusCode::NO_CONTENT)
}

pub(crate) async fn read(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Option<Permit>>, Failure> {
    let (token, user) = publisher_identity(&headers)?;
    let p = shared.lock().await;
    if !p.publisher_authorized(token, user, &camera) {
        return Err(Failure(StatusCode::UNAUTHORIZED));
    }
    Ok(Json(p.microphone.active(&camera, Instant::now())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{config::Config, security::GatewayPolicy};
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::sync::Arc;
    use tokio::sync::Mutex;

    fn headers(digit: char) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(
            "authorization",
            format!("Bearer {}", digit.to_string().repeat(64))
                .parse()
                .unwrap(),
        );
        headers.insert("x-nxrs-publisher", "publisher".parse().unwrap());
        headers
    }

    #[tokio::test]
    async fn capture_permission_is_scoped_expires_and_is_revoked() {
        let hash = |digit: char| format!("{:x}", Sha256::digest(digit.to_string().repeat(64)));
        let cameras = ["one", "two"].map(|id| json!({
            "camera_id": id, "site_id": "lab", "gateway_id": "gw", "name": id, "source": "synthetic",
            "stream_path": id, "whep_endpoint": format!("https://media.test/{id}/whep")
        }));
        let config: Config = serde_json::from_value(json!({
            "listen": "127.0.0.1:18080", "auth_listen": "127.0.0.1:18081", "grant_ttl_seconds": 10,
            "max_grants": 4, "media_origins": ["https://media.test"], "cameras": cameras,
            "viewers": [
                {"viewer_id": "controller", "scope": "microphone_control", "site_id": "lab", "camera_ids": ["one"], "token_sha256": hash('a')},
                {"viewer_id": "viewer", "site_id": "lab", "camera_ids": ["one"], "token_sha256": hash('b')}
            ],
            "publishers": [
                {"username": "publisher", "camera_id": "one", "token_sha256": hash('c')}
            ]
        })).unwrap();
        let shared = Arc::new(Mutex::new(GatewayPolicy::new(config, None).unwrap()));
        let change = |principal, camera: &str, permit| {
            write(
                State(shared.clone()),
                Path(camera.into()),
                headers(principal),
                Ok(Json(Change { permit })),
            )
        };
        let inspect = |principal, camera: &str| {
            read(
                State(shared.clone()),
                Path(camera.into()),
                headers(principal),
            )
        };
        assert_eq!(inspect('c', "one").await.unwrap().0, None);
        assert_eq!(
            change('b', "one", Some([1; 16])).await.unwrap_err().0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            change('a', "two", Some([1; 16])).await.unwrap_err().0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            inspect('c', "two").await.unwrap_err().0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            inspect('a', "one").await.unwrap_err().0,
            StatusCode::UNAUTHORIZED
        );

        let start = Instant::now();
        assert_eq!(
            change('a', "one", Some([1; 16])).await.unwrap(),
            StatusCode::NO_CONTENT
        );
        let active = inspect('c', "one").await.unwrap().0.unwrap();
        assert_eq!(active.id, [1; 16]);
        assert!((1..=LEASE.as_millis() as u64).contains(&active.valid_for_ms));
        assert_eq!(
            shared
                .lock()
                .await
                .microphone
                .active("one", start + LEASE + Duration::from_secs(1)),
            None
        );
        change('a', "one", None).await.unwrap();
        assert_eq!(inspect('c', "one").await.unwrap().0, None);
        change('a', "one", Some([2; 16])).await.unwrap();
        shared.lock().await.revoke("controller").unwrap();
        assert_eq!(inspect('c', "one").await.unwrap().0, None);
        assert_eq!(
            change('a', "one", Some([3; 16])).await.unwrap_err().0,
            StatusCode::FORBIDDEN
        );
    }
}

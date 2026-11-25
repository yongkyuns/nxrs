//! A short capture lease owned by the camera's authorized privacy controller.
use crate::{bearer, publisher_identity, Failure, Shared};
use axum::{
    extract::{rejection::JsonRejection, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

const LEASE: Duration = Duration::from_millis(1500);
const CONFIRMATION: Duration = Duration::from_millis(600);
type CommandId = [u8; 16];

#[derive(Debug, Eq, PartialEq, Serialize)]
pub(crate) struct Permit {
    id: CommandId,
    valid_for_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    muted: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    hold_audio: Option<bool>,
}

#[derive(Default)]
pub(crate) struct Leases(HashMap<String, Lease>);

struct Lease {
    id: CommandId,
    muted: Option<bool>,
    hold_audio: bool,
    deadline: Instant,
    confirmed_until: Option<Instant>,
}

impl Leases {
    pub(crate) fn clear(&mut self, camera: &str) {
        self.0.remove(camera);
    }

    fn renew(
        &mut self,
        camera: String,
        id: CommandId,
        muted: Option<bool>,
        hold_audio: bool,
        now: Instant,
    ) -> bool {
        let lease = self.0.entry(camera).or_insert(Lease {
            id,
            muted,
            hold_audio,
            deadline: now,
            confirmed_until: None,
        });
        if lease.id != id || lease.muted != muted || lease.deadline <= now {
            lease.id = id;
            lease.muted = muted;
            lease.confirmed_until = None;
        }
        lease.hold_audio = hold_audio;
        lease.deadline = now + LEASE;
        lease.confirmed_until.is_some_and(|end| end > now)
    }

    fn active(&mut self, camera: &str, now: Instant, report: Option<Report>) -> Option<Permit> {
        let lease = self.0.get_mut(camera)?;
        let valid_for_ms = lease.deadline.saturating_duration_since(now).as_millis() as u64;
        if valid_for_ms == 0 {
            return None;
        }
        if let Some(report) = report {
            lease.confirmed_until =
                (report.applied == Some(lease.id)).then_some(now + CONFIRMATION);
        } else if lease.muted.is_some() {
            // An older ingest cannot acknowledge the command or distinguish mute from a permit.
            return None;
        }
        Some(Permit {
            id: lease.id,
            valid_for_ms,
            muted: lease.muted,
            hold_audio: lease.muted.map(|_| lease.hold_audio),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Change {
    permit: Option<CommandId>,
    command_id: Option<CommandId>,
    #[serde(default)]
    hold_audio: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Report {
    version: u8,
    applied: Option<CommandId>,
}

pub(crate) async fn write(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    payload: Result<Json<Change>, JsonRejection>,
) -> Result<Response, Failure> {
    let mut p = shared.lock().await;
    p.microphone_controller(bearer(&headers)?, &camera)
        .map_err(Failure)?;
    let Json(change) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if let Some(id) = change.command_id {
        if change.permit.is_some_and(|permit| permit != id)
            || (change.hold_audio && change.permit.is_none())
        {
            return Err(Failure(StatusCode::BAD_REQUEST));
        }
        let applied = p.microphone.renew(
            camera,
            id,
            Some(change.permit.is_none()),
            change.hold_audio,
            Instant::now(),
        );
        return Ok(Json(applied).into_response());
    }
    if change.hold_audio {
        return Err(Failure(StatusCode::BAD_REQUEST));
    }
    if let Some(permit) = change.permit {
        p.microphone
            .renew(camera, permit, None, false, Instant::now());
    } else {
        p.microphone.clear(&camera);
    }
    Ok(StatusCode::NO_CONTENT.into_response())
}

pub(crate) async fn read(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    report: Result<Json<Report>, JsonRejection>,
) -> Result<Json<Option<Permit>>, Failure> {
    let (token, user) = publisher_identity(&headers)?;
    let mut p = shared.lock().await;
    if !p.publisher_authorized(token, user, &camera) {
        return Err(Failure(StatusCode::UNAUTHORIZED));
    }
    let report = report
        .ok()
        .map(|Json(report)| report)
        .filter(|report| report.version == 2);
    Ok(Json(p.microphone.active(&camera, Instant::now(), report)))
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
                Ok(Json(Change {
                    permit,
                    command_id: None,
                    hold_audio: false,
                })),
            )
        };
        let inspect = |principal, camera: &str| {
            read(
                State(shared.clone()),
                Path(camera.into()),
                headers(principal),
                Ok(Json(Report {
                    version: 1,
                    applied: None,
                })),
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
            change('a', "one", Some([1; 16])).await.unwrap().status(),
            StatusCode::NO_CONTENT
        );
        let active = inspect('c', "one").await.unwrap().0.unwrap();
        assert_eq!(active.id, [1; 16]);
        assert!((1..=LEASE.as_millis() as u64).contains(&active.valid_for_ms));
        assert_eq!(
            shared.lock().await.microphone.active(
                "one",
                start + LEASE + Duration::from_secs(1),
                None
            ),
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

    #[test]
    fn stale_and_legacy_publishers_cannot_confirm_a_new_command() {
        let now = Instant::now();
        let mut leases = Leases::default();
        let report = |id| {
            Some(Report {
                version: 2,
                applied: Some([id; 16]),
            })
        };
        assert!(!leases.renew("one".into(), [1; 16], Some(false), true, now));
        assert!(leases.active("one", now, None).is_none());
        assert_eq!(
            leases.active("one", now, report(1)).unwrap().hold_audio,
            Some(true)
        );
        assert!(leases.renew("one".into(), [1; 16], Some(false), false, now));
        assert_eq!(
            leases.active("one", now, report(1)).unwrap().hold_audio,
            Some(false)
        );
        assert!(!leases.renew("one".into(), [2; 16], Some(true), false, now));
        assert_eq!(
            leases.active("one", now, report(1)).unwrap().muted,
            Some(true)
        );
        assert!(!leases.renew("one".into(), [2; 16], Some(true), false, now));
        assert!(leases.active("one", now, report(2)).is_some());
        assert!(leases.renew("one".into(), [2; 16], Some(true), false, now));
        assert!(!leases.renew("one".into(), [2; 16], Some(true), false, now + CONFIRMATION));
    }
}

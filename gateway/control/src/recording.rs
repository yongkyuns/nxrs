//! Camera-scoped access to the native recorder's bounded permission.
use crate::{bearer, media_control, security::Settings, Failure, Shared};
use axum::{
    extract::{rejection::JsonRejection, Path, State},
    http::{HeaderMap, Method, StatusCode},
    Json,
};
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

pub(crate) struct Target {
    pub path: String,
    pub settings: Settings,
    pub slot: Arc<Mutex<()>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Permit {
    path_id: String,
    revision: u64,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct NativePath {
    name: String,
    recording_enabled: bool,
    recorder_present: bool,
    recording_permit: Permit,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Change {
    enabled: bool,
}

#[derive(Serialize)]
pub(crate) struct Status {
    enabled: bool,
}

const UNAVAILABLE: Failure = Failure(StatusCode::SERVICE_UNAVAILABLE);
// Match the native recorder's permission limit. Start accounting before the
// native exchange so detection can never inherit extra time from its reply.
const PERMISSION: Duration = Duration::from_secs(3);

impl Target {
    async fn path(&self) -> Result<NativePath, Failure> {
        let (status, body) = media_control::exchange(
            &self.settings,
            Method::GET,
            &format!("/v3/paths/get/{}", self.path),
            None,
        )
        .await
        .map_err(|_| UNAVAILABLE)?;
        if status != StatusCode::OK {
            return Err(UNAVAILABLE);
        }
        let path: NativePath = serde_json::from_slice(&body).map_err(|_| UNAVAILABLE)?;
        if path.name != self.path
            || !crate::security::session_id(&path.recording_permit.path_id)
            || path.recording_permit.path_id == "00000000-0000-0000-0000-000000000000"
        {
            return Err(UNAVAILABLE);
        }
        Ok(path)
    }

    async fn apply(&self, expected: &Permit, enabled: bool) -> Result<Permit, Failure> {
        let revision = expected.revision.checked_add(1).ok_or(UNAVAILABLE)?;
        let body = serde_json::json!({
            "pathId": expected.path_id, "revision": expected.revision, "enable": enabled,
        });
        let (status, body) = media_control::exchange(
            &self.settings,
            Method::POST,
            &format!("/v3/paths/recording-permit/{}", self.path),
            Some(Bytes::from(body.to_string())),
        )
        .await
        .map_err(|_| UNAVAILABLE)?;
        if status != StatusCode::OK {
            return Err(UNAVAILABLE);
        }
        let applied: Permit = serde_json::from_slice(&body).map_err(|_| UNAVAILABLE)?;
        if applied.path_id != expected.path_id || applied.revision != revision {
            return Err(UNAVAILABLE);
        }
        Ok(applied)
    }
}

async fn target(shared: &Shared, token: &str, camera: &str) -> Result<Target, Failure> {
    shared
        .lock()
        .await
        .recording_target(token, camera)
        .map_err(Failure)
}

pub(crate) async fn read(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Status>, Failure> {
    let token = bearer(&headers)?;
    let recorder = target(&shared, token, &camera).await?;
    let _slot = recorder.slot.try_lock().map_err(|_| UNAVAILABLE)?;
    let path = recorder.path().await?;
    target(&shared, token, &camera).await?;
    // A path can report expiry while its recorder is still draining.
    if !path.recording_enabled && path.recorder_present {
        return Err(UNAVAILABLE);
    }
    Ok(Json(Status {
        enabled: path.recording_enabled,
    }))
}

pub(crate) async fn write(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    payload: Result<Json<Change>, JsonRejection>,
) -> Result<StatusCode, Failure> {
    let token = bearer(&headers)?;
    let recorder = target(&shared, token, &camera).await?;
    let Json(change) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    let _slot = recorder.slot.try_lock().map_err(|_| UNAVAILABLE)?;
    if !change.enabled {
        shared
            .lock()
            .await
            .detections
            .set_analysis(&camera, None, Instant::now());
    }
    let path = recorder.path().await?;
    // Check revocation again after the native lookup, without holding the
    // policy lock through I/O. MediaMTX uses that lock to authorize its API call.
    target(&shared, token, &camera).await?;
    let began = Instant::now();
    let applied = recorder
        .apply(&path.recording_permit, change.enabled)
        .await?;
    let mut policy = shared.lock().await;
    if let Err(status) = policy.recording_target(token, &camera) {
        drop(policy);
        if change.enabled {
            let _ = recorder.apply(&applied, false).await;
        }
        return Err(Failure(status));
    }
    if change.enabled {
        policy
            .detections
            .set_analysis(&camera, Some(began + PERMISSION), Instant::now());
    }
    Ok(StatusCode::NO_CONTENT)
}

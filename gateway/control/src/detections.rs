//! Bounded camera metadata, independent of media distribution and encoder work.
use crate::{bearer, config::Config, Failure, Shared};
use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::sse::{Event, Sse},
    Json,
};
use detection_wire::{self as wire, Presence, Snapshot, Update, Version};
use futures_util::{stream, Stream};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

struct CameraState {
    session: Option<String>,
    update: Option<Update>,
    received: Option<Instant>,
    registered: Option<Instant>,
    last_observation: Option<wire::Observation>,
    revision: u64,
}
pub(crate) struct Store {
    pub(crate) shutting_down: bool,
    cameras: HashMap<String, CameraState>,
    viewers: HashMap<String, Arc<Semaphore>>,
    total: Arc<Semaphore>,
}
impl Store {
    pub fn new(c: &Config) -> Self {
        Self {
            shutting_down: false,
            cameras: c
                .cameras
                .iter()
                .map(|c| {
                    (
                        c.camera_id.clone(),
                        CameraState {
                            session: None,
                            update: None,
                            received: None,
                            registered: None,
                            last_observation: None,
                            revision: 0,
                        },
                    )
                })
                .collect(),
            viewers: c
                .viewers
                .iter()
                .map(|v| (v.viewer_id.clone(), Arc::new(Semaphore::new(4))))
                .collect(),
            total: Arc::new(Semaphore::new(64)),
        }
    }
    fn subscribe(
        &self,
        viewer: &str,
    ) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit), StatusCode> {
        let a = self
            .total
            .clone()
            .try_acquire_owned()
            .map_err(|_| StatusCode::TOO_MANY_REQUESTS)?;
        let b = self
            .viewers
            .get(viewer)
            .ok_or(StatusCode::UNAUTHORIZED)?
            .clone()
            .try_acquire_owned()
            .map_err(|_| StatusCode::TOO_MANY_REQUESTS)?;
        Ok((a, b))
    }
    pub(crate) fn register(&mut self, camera: &str, now: Instant) -> Result<String, StatusCode> {
        let c = self.cameras.get_mut(camera).ok_or(StatusCode::NOT_FOUND)?;
        if c.registered
            .is_some_and(|v| now.saturating_duration_since(v) < Duration::from_millis(250))
        {
            return Err(StatusCode::TOO_MANY_REQUESTS);
        }
        let mut bytes = [0u8; 16];
        getrandom::fill(&mut bytes).map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
        let id: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        c.session = Some(id.clone());
        c.update = None;
        c.received = None;
        c.last_observation = None;
        c.registered = Some(now);
        c.revision = c.revision.saturating_add(1);
        Ok(id)
    }
    pub(crate) fn update(
        &mut self,
        camera: &str,
        v: Update,
        now: Instant,
    ) -> Result<(), StatusCode> {
        let c = self.cameras.get_mut(camera).ok_or(StatusCode::NOT_FOUND)?;
        if c.session.as_ref() != Some(&v.source_session_id) {
            return Err(StatusCode::CONFLICT);
        }
        v.source.validate().map_err(|_| StatusCode::BAD_REQUEST)?;
        if v.frame_sequence == 0
            || v.source.emitted_ms > v.frame_capture_ms.saturating_add(1000)
            || v.source.latest.as_ref().is_some_and(|o| {
                o.frame_sequence > v.frame_sequence || o.captured_ms > v.frame_capture_ms
            })
            || c.update.as_ref().is_some_and(|old| {
                v.frame_sequence <= old.frame_sequence
                    || v.frame_capture_ms < old.frame_capture_ms
                    || v.source.publication_id < old.source.publication_id
                    || v.source.emitted_ms < old.source.emitted_ms
                    || v.source.event_count < old.source.event_count
                    || (v.source.publication_id == old.source.publication_id
                        && v.source != old.source)
            })
            || v.source
                .latest
                .as_ref()
                .zip(c.last_observation.as_ref())
                .is_some_and(|(a, b)| {
                    a.frame_sequence < b.frame_sequence
                        || a.captured_ms < b.captured_ms
                        || (a.frame_sequence == b.frame_sequence && a != b)
                })
        {
            return Err(StatusCode::BAD_REQUEST);
        }
        if let Some(observation) = &v.source.latest {
            c.last_observation = Some(observation.clone());
        }
        c.update = Some(v);
        c.received = Some(now);
        c.revision = c.revision.saturating_add(1);
        Ok(())
    }
    pub(crate) fn snapshot(&self, camera: &str, now: Instant, cursor: Option<&str>) -> Snapshot {
        let c = &self.cameras[camera];
        let elapsed = c
            .received
            .map(|v| now.saturating_duration_since(v).as_millis() as u64);
        let online = elapsed.is_some_and(|v| v < wire::SOURCE_TIMEOUT_MS);
        let source = c.update.as_ref().map(|u| u.source.clone());
        let age = c.update.as_ref().and_then(|u| {
            u.source.latest.as_ref().map(|o| {
                u.frame_capture_ms
                    .max(u.source.emitted_ms)
                    .saturating_sub(o.captured_ms)
                    .saturating_add(elapsed.unwrap_or(u64::MAX))
            })
        });
        let presence = if online && age.is_some_and(|a| a < wire::MAX_AGE_MS) {
            source
                .as_ref()
                .map_or(Presence::Unavailable, |s| s.presence)
        } else {
            Presence::Unavailable
        };
        let event_gap = cursor.is_some_and(|cursor| {
            let Some((session, count)) = cursor.split_once(':') else {
                return true;
            };
            let Ok(count) = count.parse::<u64>() else {
                return true;
            };
            (c.session.as_deref() != Some(session) && !(c.session.is_none() && session == "none"))
                || source.as_ref().is_some_and(|s| {
                    count > s.event_count
                        || s.events
                            .iter()
                            .next()
                            .is_some_and(|e| e.id > count.saturating_add(1))
                })
        });
        Snapshot {
            api_version: Version::V1,
            camera_id: camera.into(),
            source_session_id: c.session.clone(),
            revision: c.revision,
            source_online: online,
            presence,
            stream_elapsed_ms: 0,
            observation_age_ms: age,
            valid_for_ms: 1500,
            frame_synchronized: false,
            source,
            event_gap,
        }
    }
}
fn publisher(headers: &HeaderMap) -> Result<(&str, &str), Failure> {
    let user = headers
        .get("x-nxrs-publisher")
        .and_then(|v| v.to_str().ok())
        .filter(|v| crate::config::identifier(v))
        .ok_or(Failure(StatusCode::UNAUTHORIZED))?;
    Ok((bearer(headers)?, user))
}
pub(crate) async fn register(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Value>, Failure> {
    let (token, user) = publisher(&headers)?;
    let mut p = shared.lock().await;
    if !p.detection_publisher(token, user, &camera) {
        return Err(Failure(StatusCode::UNAUTHORIZED));
    }
    let id = p
        .detections
        .register(&camera, Instant::now())
        .map_err(Failure)?;
    Ok(Json(json!({"source_session_id":id})))
}
pub(crate) async fn publish(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    payload: Result<Json<Update>, axum::extract::rejection::JsonRejection>,
) -> Result<StatusCode, Failure> {
    let (token, user) = publisher(&headers)?;
    let mut p = shared.lock().await;
    if !p.detection_publisher(token, user, &camera) {
        return Err(Failure(StatusCode::UNAUTHORIZED));
    }
    let Json(v) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    p.detections
        .update(&camera, v, Instant::now())
        .map_err(Failure)?;
    Ok(StatusCode::NO_CONTENT)
}
pub(crate) async fn subscribe(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, Failure> {
    let token = bearer(&headers)?.to_owned();
    let cursor = headers
        .get("last-event-id")
        .map(|v| v.to_str().unwrap_or("").to_owned());
    if cursor.as_ref().is_some_and(|v| {
        v.len() > 100 || !v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b':')
    }) {
        return Err(Failure(StatusCode::BAD_REQUEST));
    }
    let permits = {
        let p = shared.lock().await;
        let viewer = p.detection_viewer(&token, &camera).map_err(Failure)?;
        p.detections.subscribe(&viewer).map_err(Failure)?
    };
    let started = Instant::now();
    let events = stream::unfold(
        (shared, camera, token, cursor, permits, started, false),
        |(shared, camera, token, cursor, permits, started, wait)| async move {
            if wait {
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            // End authorization on every emission, including current durable denial.
            let p = shared.lock().await;
            if p.detections.shutting_down || p.detection_viewer(&token, &camera).is_err() {
                return None;
            }
            let mut s = p
                .detections
                .snapshot(&camera, Instant::now(), cursor.as_deref());
            drop(p);
            s.stream_elapsed_ms = started.elapsed().as_millis() as u64;
            let id = format!(
                "{}:{}",
                s.source_session_id.as_deref().unwrap_or("none"),
                s.source.as_ref().map_or(0, |v| v.event_count)
            );
            let event = Event::default()
                .event("detection")
                .id(&id)
                .json_data(&s)
                .ok()?;
            Some((
                Ok(event),
                (shared, camera, token, Some(id), permits, started, true),
            ))
        },
    );
    Ok(Sse::new(events))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{config::Config, security::GatewayPolicy};
    use axum::response::IntoResponse;
    use futures_util::StreamExt;
    use sha2::{Digest, Sha256};
    fn config() -> Config {
        let digest = |s: &str| -> String {
            Sha256::digest(s.as_bytes())
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect()
        };
        serde_json::from_value(json!({"listen":"127.0.0.1:18080","auth_listen":"127.0.0.1:18081",
            "grant_ttl_seconds":60,"max_grants":64,"media_origins":["https://media.test"],
            "cameras":[{"camera_id":"cam_one","gateway_id":"gw","site_id":"site","name":"Test","source":"synthetic","stream_path":"one","whep_endpoint":"https://media.test/one/whep"}],
            "viewers":[{"viewer_id":"viewer","site_id":"site","camera_ids":["cam_one"],"token_sha256":digest(&"a".repeat(64))}],
            "publishers":[{"username":"publisher","camera_id":"cam_one","token_sha256":digest(&"b".repeat(64))}]})).unwrap()
    }
    fn source() -> wire::Source {
        let s: Snapshot = serde_json::from_slice(include_bytes!(
            "../../../clients/ios/Tests/NxrsMonitorCoreTests/Fixtures/detection.json"
        ))
        .unwrap();
        s.source.unwrap()
    }
    fn update(id: String, frame: u64) -> Update {
        Update {
            source_session_id: id,
            frame_sequence: frame,
            frame_capture_ms: 1100,
            source: source(),
        }
    }
    #[test]
    fn source_expiry_replay_session_replacement_and_event_gaps() {
        let mut store = Store::new(&config());
        let t = Instant::now();
        let id = store.register("cam_one", t).unwrap();
        let v = update(id.clone(), 20);
        store.update("cam_one", v.clone(), t).unwrap();
        assert_eq!(
            store.snapshot("cam_one", t, None).presence,
            Presence::Person
        );
        assert_eq!(
            store
                .snapshot("cam_one", t + Duration::from_millis(1500), None)
                .presence,
            Presence::Unavailable
        );
        assert!(store.update("cam_one", v, t).is_err());
        let mut v = update(id.clone(), 21);
        v.source.latest.as_mut().unwrap().objects.0[0]
            .as_mut()
            .unwrap()
            .bounds
            .left = 999;
        assert!(store.update("cam_one", v, t).is_err());
        let mut v = update(id.clone(), 21);
        v.frame_capture_ms = 7000;
        store.update("cam_one", v, t).unwrap();
        assert_eq!(
            store.snapshot("cam_one", t, None).presence,
            Presence::Unavailable
        );
        let later = t + Duration::from_secs(1);
        let new = store.register("cam_one", later).unwrap();
        assert_ne!(new, id);
        assert!(
            store
                .snapshot("cam_one", later, Some(&format!("{id}:1")))
                .event_gap
        );
        assert!(store.update("cam_one", update(id, 22), later).is_err());
        assert_eq!(
            store.snapshot("cam_one", later, None).presence,
            Presence::Unavailable
        );
    }
    #[tokio::test]
    async fn live_stream_authorization_revocation_and_bounds() {
        let shared = Arc::new(tokio::sync::Mutex::new(
            GatewayPolicy::new(config(), None).unwrap(),
        ));
        let headers = |token: &str| {
            let mut h = HeaderMap::new();
            h.insert("authorization", format!("Bearer {token}").parse().unwrap());
            h
        };
        assert_eq!(
            subscribe(
                State(shared.clone()),
                Path("cam_one".into()),
                headers(&"b".repeat(64))
            )
            .await
            .err()
            .unwrap()
            .0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            subscribe(
                State(shared.clone()),
                Path("other".into()),
                headers(&"a".repeat(64))
            )
            .await
            .err()
            .unwrap()
            .0,
            StatusCode::NOT_FOUND
        );
        let mut bodies = Vec::new();
        for _ in 0..4 {
            bodies.push(
                subscribe(
                    State(shared.clone()),
                    Path("cam_one".into()),
                    headers(&"a".repeat(64)),
                )
                .await
                .unwrap()
                .into_response()
                .into_body(),
            );
        }
        assert_eq!(
            subscribe(
                State(shared.clone()),
                Path("cam_one".into()),
                headers(&"a".repeat(64))
            )
            .await
            .err()
            .unwrap()
            .0,
            StatusCode::TOO_MANY_REQUESTS
        );
        bodies.pop();
        let s = subscribe(
            State(shared.clone()),
            Path("cam_one".into()),
            headers(&"a".repeat(64)),
        )
        .await
        .unwrap();
        let mut events = s.into_response().into_body().into_data_stream();
        let bytes = events.next().await.unwrap().unwrap();
        assert!(std::str::from_utf8(&bytes).unwrap().contains("unavailable"));
        shared.lock().await.revoke("viewer").unwrap();
        assert!(tokio::time::timeout(Duration::from_secs(2), events.next())
            .await
            .unwrap()
            .is_none());
        drop(bodies);
    }
    #[test]
    fn unavailable_publications_preserve_observation_replay_barrier() {
        let mut store = Store::new(&config());
        let t = Instant::now();
        let id = store.register("cam_one", t).unwrap();
        store.update("cam_one", update(id.clone(), 20), t).unwrap();
        let mut unavailable = update(id.clone(), 21);
        unavailable.source.publication_id += 1;
        unavailable.source.presence = Presence::Unavailable;
        unavailable.source.latest = None;
        store.update("cam_one", unavailable, t).unwrap();
        let mut replay = update(id, 22);
        replay.source.publication_id += 2;
        replay.source.latest.as_mut().unwrap().frame_sequence -= 1;
        assert!(store.update("cam_one", replay.clone(), t).is_err());
        replay.source.latest.as_mut().unwrap().frame_sequence += 1;
        replay.source.latest.as_mut().unwrap().person_score_permille -= 1;
        assert!(store.update("cam_one", replay.clone(), t).is_err());
        replay.source.latest.as_mut().unwrap().person_score_permille += 1;
        store.update("cam_one", replay, t).unwrap();
    }
    #[tokio::test]
    async fn shutdown_ends_idle_sse_and_releases_subscriber_permits() {
        let shared = Arc::new(tokio::sync::Mutex::new(
            GatewayPolicy::new(config(), None).unwrap(),
        ));
        let mut headers = HeaderMap::new();
        headers.insert(
            "authorization",
            format!("Bearer {}", "a".repeat(64)).parse().unwrap(),
        );
        let mut events = subscribe(State(shared.clone()), Path("cam_one".into()), headers)
            .await
            .unwrap()
            .into_response()
            .into_body()
            .into_data_stream();
        assert!(events.next().await.is_some());
        shared.lock().await.detections.shutting_down = true;
        assert!(tokio::time::timeout(Duration::from_secs(2), events.next())
            .await
            .unwrap()
            .is_none());
        let p = shared.lock().await;
        assert_eq!(p.detections.total.available_permits(), 64);
        assert_eq!(p.detections.viewers["viewer"].available_permits(), 4);
    }
    #[test]
    fn metadata_publishers_are_scoped_and_cannot_use_viewer_credentials() {
        let p = GatewayPolicy::new(config(), None).unwrap();
        assert!(p.detection_publisher(&"b".repeat(64), "publisher", "cam_one"));
        assert!(!p.detection_publisher(&"a".repeat(64), "publisher", "cam_one"));
        assert!(!p.detection_publisher(&"b".repeat(64), "publisher", "other"));
    }
}

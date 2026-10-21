#![forbid(unsafe_code)]
pub mod config;
mod policy;
mod revocations;
pub mod security;
mod media_control;
pub use media_control::Maintenance;

use axum::{
    extract::{DefaultBodyLimit, Path, Request, State},
    http::{header, HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use security::{Auth, GatewayPolicy, Settings};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{sync::Arc, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use tokio::sync::{Mutex, Semaphore};

type Shared = Arc<Mutex<GatewayPolicy>>;
// Survives cancellation of the HTTP handler: a stuck fsync cannot create an unbounded worker queue.
static REVOCATION_IO: Semaphore = Semaphore::const_new(1);
struct Failure(StatusCode);
impl IntoResponse for Failure {
    fn into_response(self) -> Response { failure(self.0) }
}
fn failure(status: StatusCode) -> Response {
    let code = match status {
        StatusCode::UNAUTHORIZED => "unauthenticated",
        StatusCode::FORBIDDEN => "forbidden",
        StatusCode::NOT_FOUND => "not_found",
        StatusCode::TOO_MANY_REQUESTS => "rate_limited",
        StatusCode::BAD_REQUEST => "invalid_request",
        _ => "service_unavailable",
    };
    let mut response = (status, Json(json!({"code": code}))).into_response();
    if status == StatusCode::UNAUTHORIZED {
        response.headers_mut().insert(header::WWW_AUTHENTICATE, "Bearer".parse().unwrap());
    }
    response
}
fn bearer(headers: &HeaderMap) -> Result<&str, Failure> {
    if headers.get_all(header::AUTHORIZATION).iter().count() != 1 { return Err(Failure(StatusCode::UNAUTHORIZED)); }
    headers.get(header::AUTHORIZATION).and_then(|h| h.to_str().ok()).and_then(|s| s.strip_prefix("Bearer "))
        .filter(|s| config::hex_token(s)).ok_or(Failure(StatusCode::UNAUTHORIZED))
}
fn wall() -> Result<u64, Failure> {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|t| t.as_secs())
        .map_err(|_| Failure(StatusCode::SERVICE_UNAVAILABLE))
}
async fn health() -> Json<Value> { Json(json!({"status": "ok"})) }
async fn catalog(State(shared): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, Failure> {
    shared.lock().await.catalog(bearer(&headers)?).map(Json).map_err(Failure)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}
async fn grant(
    State(shared): State<Shared>, Path(camera): Path<String>, headers: HeaderMap,
    payload: Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<(StatusCode, Json<Value>), Failure> {
    let token = bearer(&headers)?;
    let Json(_) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if !config::identifier(&camera) { return Err(Failure(StatusCode::NOT_FOUND)); }
    let grant = shared.lock().await.issue(token, &camera, wall()?, Instant::now()).map_err(Failure)?;
    Ok((StatusCode::CREATED, Json(grant)))
}
async fn media_auth(
    State(shared): State<Shared>, payload: Result<Json<Auth>, axum::extract::rejection::JsonRejection>,
) -> Result<StatusCode, Failure> {
    let Json(auth) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if shared.lock().await.authorize_media(&auth, wall()?, Instant::now()) { Ok(StatusCode::NO_CONTENT) }
    else { Err(Failure(StatusCode::UNAUTHORIZED)) }
}
async fn revoke(
    State(shared): State<Shared>, Path(viewer): Path<String>,
    payload: Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<StatusCode, Failure> {
    let Json(_) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    let permit = REVOCATION_IO.try_acquire().map_err(|_| Failure(StatusCode::TOO_MANY_REQUESTS))?;
    // Filesystem synchronization must not block an async runtime thread. If the HTTP client cancels,
    // the admitted denial continues; it is not undone because a response could not be delivered.
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        shared.blocking_lock().revoke(&viewer)
    }).await.map_err(|_| Failure(StatusCode::SERVICE_UNAVAILABLE))?.map_err(Failure)
}
async fn revocation_status(State(shared): State<Shared>, Path(viewer): Path<String>) -> Result<Json<Value>, Failure> {
    shared.lock().await.revocation_status(&viewer, Instant::now()).map(Json).map_err(Failure)
}
async fn guard(State(admission): State<Arc<Semaphore>>, request: Request, next: Next) -> Response {
    let mut response = if request.uri().query().is_some() { failure(StatusCode::BAD_REQUEST) }
    else if let Ok(_permit) = admission.try_acquire_owned() {
        match tokio::time::timeout(Duration::from_secs(5), next.run(request)).await {
            Ok(response) => response, Err(_) => failure(StatusCode::REQUEST_TIMEOUT),
        }
    } else { failure(StatusCode::TOO_MANY_REQUESTS) };
    response.headers_mut().insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response.headers_mut().insert("x-content-type-options", "nosniff".parse().unwrap());
    response
}
fn guarded(router: Router) -> Router {
    router.fallback(|| async { failure(StatusCode::NOT_FOUND) })
        .layer(DefaultBodyLimit::max(16 * 1024))
        .layer(middleware::from_fn_with_state(Arc::new(Semaphore::new(64)), guard))
}
/// Legacy development configuration remains supported; durable enforcement is explicitly enabled.
pub fn routers(config: config::Config) -> Result<(Router, Router), &'static str> {
    let (control, internal, _) = managed_routers(config, None)?;
    Ok((control, internal))
}
pub fn managed_routers(config: config::Config, settings: Option<Settings>) -> Result<(Router, Router, Maintenance), &'static str> {
    let shared = Arc::new(Mutex::new(GatewayPolicy::new(config, settings.clone())?));
    let control = Router::new().route("/healthz", get(health)).route("/v1/cameras", get(catalog))
        .route("/v1/cameras/{camera}/playback-grants", post(grant)).with_state(shared.clone());
    let internal = Router::new().route("/internal/mediamtx/auth", post(media_auth))
        .route("/internal/viewers/{viewer}/revoke", post(revoke))
        .route("/internal/viewers/{viewer}/revocation", get(revocation_status)).with_state(shared.clone());
    Ok((guarded(control), guarded(internal), Maintenance::new(shared, settings)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cancellation_cannot_release_a_running_persistence_slot() {
        let permit = REVOCATION_IO.try_acquire().unwrap();
        let (entered, ready) = tokio::sync::oneshot::channel();
        let (finish, release) = std::sync::mpsc::channel();
        let worker = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            let _ = entered.send(());
            release.recv_timeout(Duration::from_secs(3)).unwrap();
        });
        ready.await.unwrap();
        worker.abort();
        assert!(REVOCATION_IO.try_acquire().is_err());
        finish.send(()).unwrap();
        let _ = worker.await;
        assert!(REVOCATION_IO.try_acquire().is_ok());
    }
}

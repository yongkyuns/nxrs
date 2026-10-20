#![forbid(unsafe_code)]
pub mod config;
mod policy;

use axum::{
    extract::{DefaultBodyLimit, Path, Request, State},
    http::{header, HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use policy::{MediaAuth, Policy};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;

type Shared = Arc<Mutex<Policy>>;
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
    if headers.get_all(header::AUTHORIZATION).iter().count() != 1 {
        return Err(Failure(StatusCode::UNAUTHORIZED));
    }
    headers.get(header::AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .filter(|s| config::hex_token(s))
        .ok_or(Failure(StatusCode::UNAUTHORIZED))
}

fn wall() -> Result<u64, Failure> {
    SystemTime::now().duration_since(UNIX_EPOCH)
        .map(|t| t.as_secs())
        .map_err(|_| Failure(StatusCode::SERVICE_UNAVAILABLE))
}

fn locked(shared: &Shared) -> Result<std::sync::MutexGuard<'_, Policy>, Failure> {
    shared.lock().map_err(|_| Failure(StatusCode::SERVICE_UNAVAILABLE))
}

async fn health() -> Json<Value> { Json(json!({"status": "ok"})) }

async fn catalog(State(shared): State<Shared>, headers: HeaderMap) -> Result<Json<Value>, Failure> {
    locked(&shared)?.catalog(bearer(&headers)?).map(Json).map_err(Failure)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}

async fn grant(
    State(shared): State<Shared>,
    Path(camera): Path<String>,
    headers: HeaderMap,
    payload: Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<(StatusCode, Json<Value>), Failure> {
    let token = bearer(&headers)?;
    let Json(_) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if !config::identifier(&camera) { return Err(Failure(StatusCode::NOT_FOUND)); }
    let grant = locked(&shared)?.issue(token, &camera, wall()?, Instant::now()).map_err(Failure)?;
    Ok((StatusCode::CREATED, Json(grant)))
}

async fn media_auth(
    State(shared): State<Shared>,
    payload: Result<Json<MediaAuth>, axum::extract::rejection::JsonRejection>,
) -> Result<StatusCode, Failure> {
    let Json(auth) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    if locked(&shared)?.authorize_media(&auth, wall()?, Instant::now()) {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(Failure(StatusCode::UNAUTHORIZED))
    }
}

async fn revoke(
    State(shared): State<Shared>,
    Path(viewer): Path<String>,
    payload: Result<Json<Empty>, axum::extract::rejection::JsonRejection>,
) -> Result<StatusCode, Failure> {
    let Json(_) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    locked(&shared)?.revoke(&viewer).map_err(Failure)?;
    Ok(StatusCode::NO_CONTENT)
}

async fn guard(State(admission): State<Arc<Semaphore>>, request: Request, next: Next) -> Response {
    // Bound handlers/body accumulation. A trusted front proxy must also bound sockets/headers.
    let mut response = if request.uri().query().is_some() {
        failure(StatusCode::BAD_REQUEST)
    } else if let Ok(_permit) = admission.try_acquire_owned() {
        match tokio::time::timeout(Duration::from_secs(5), next.run(request)).await {
            Ok(response) => response,
            Err(_) => failure(StatusCode::REQUEST_TIMEOUT),
        }
    } else {
        failure(StatusCode::TOO_MANY_REQUESTS)
    };
    response.headers_mut().insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response.headers_mut().insert("x-content-type-options", "nosniff".parse().unwrap());
    response
}

fn guarded(router: Router) -> Router {
    router.fallback(|| async { failure(StatusCode::NOT_FOUND) })
        .layer(DefaultBodyLimit::max(16 * 1024))
        .layer(middleware::from_fn_with_state(Arc::new(Semaphore::new(64)), guard))
}

/// Public routes and private callback/revocation routes use separate loopback listeners.
pub fn routers(config: config::Config) -> Result<(Router, Router), &'static str> {
    let shared = Arc::new(Mutex::new(Policy::new(config)?));
    let control = Router::new()
        .route("/healthz", get(health))
        .route("/v1/cameras", get(catalog))
        .route("/v1/cameras/{camera}/playback-grants", post(grant))
        .with_state(shared.clone());
    let internal = Router::new()
        .route("/internal/mediamtx/auth", post(media_auth))
        .route("/internal/viewers/{viewer}/revoke", post(revoke))
        .with_state(shared);
    Ok((guarded(control), guarded(internal)))
}

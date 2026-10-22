//! Account-scoped history. Raw recording directories and recorder credentials are never public.
use crate::{bearer, Failure, Shared};
use axum::{body::Body, extract::{Path, State}, http::{header, HeaderMap, StatusCode}, response::Response,
    routing::{get, post, delete}, Json, Router};
use nxrs_camera_activity::{now_ms, store::{Error, Store}};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::sync::Semaphore;

struct Context { policy: Shared, store: Option<Store>, io: Arc<Semaphore> }
type ContextRef = Arc<Context>;
fn failure(e: Error) -> Failure {
    Failure(match e { Error::NotFound => StatusCode::NOT_FOUND, Error::Busy => StatusCode::TOO_MANY_REQUESTS,
        _ => StatusCode::SERVICE_UNAVAILABLE })
}
async fn scoped(context: &Context, headers: &HeaderMap, camera: &str, manage: bool) -> Result<bool, Failure> {
    context.policy.lock().await.activity_scope(bearer(headers)?, camera, manage).map_err(Failure)
}
async fn io<T: Send + 'static>(context: &Context, work: impl FnOnce(Store) -> Result<T, Error> + Send + 'static) -> Result<T, Failure> {
    let permit = context.io.clone().try_acquire_owned().map_err(|_| Failure(StatusCode::TOO_MANY_REQUESTS))?;
    let store = context.store.clone().ok_or(Failure(StatusCode::NOT_FOUND))?;
    tokio::task::spawn_blocking(move || { let _permit = permit; work(store) })
        .await.map_err(|_| Failure(StatusCode::SERVICE_UNAVAILABLE))?.map_err(failure)
}
async fn page(context: ContextRef, camera: String, before: Option<String>, headers: HeaderMap) -> Result<Json<Value>, Failure> {
    let can_manage = scoped(&context, &headers, &camera, false).await?;
    let key = camera.clone();
    let (page, health, retention) = io(&context, move |store| {
        Ok((store.page(&key, before.as_deref())?, store.health(&key)?, store.settings().retention_seconds))
    }).await?;
    scoped(&context, &headers, &camera, false).await?;
    Ok(Json(json!({"api_version":"1", "camera_id":camera, "events":page.events, "next_cursor":page.next_cursor,
        "can_manage":can_manage, "used_bytes":page.used_bytes, "max_bytes":page.max_bytes,
        "retention_seconds":retention, "health":health, "observed_at_ms":now_ms()})))
}
async fn first(State(context): State<ContextRef>, Path(camera): Path<String>, headers: HeaderMap) -> Result<Json<Value>, Failure> {
    page(context, camera, None, headers).await
}
async fn next(State(context): State<ContextRef>, Path((camera, cursor)): Path<(String, String)>, headers: HeaderMap) -> Result<Json<Value>, Failure> {
    page(context, camera, Some(cursor), headers).await
}
async fn media(context: ContextRef, camera: String, id: String, headers: HeaderMap, thumbnail: bool) -> Result<Response, Failure> {
    scoped(&context, &headers, &camera, false).await?;
    let key = camera.clone();
    let bytes = io(&context, move |store| store.media(&key, &id, thumbnail)).await?;
    scoped(&context, &headers, &camera, false).await?;
    let mut response = Response::new(Body::from(bytes));
    response.headers_mut().insert(header::CONTENT_TYPE, if thumbnail { "image/jpeg" } else { "video/mp4" }.parse().unwrap());
    Ok(response)
}
async fn clip(State(c): State<ContextRef>, Path((camera,id)): Path<(String,String)>, headers: HeaderMap) -> Result<Response, Failure> {
    media(c, camera, id, headers, false).await
}
async fn thumbnail(State(c): State<ContextRef>, Path((camera,id)): Path<(String,String)>, headers: HeaderMap) -> Result<Response, Failure> {
    media(c, camera, id, headers, true).await
}
async fn clip_part(State(c): State<ContextRef>, Path((camera,id,part)): Path<(String,String,String)>, headers: HeaderMap) -> Result<Response, Failure> {
    scoped(&c, &headers, &camera, false).await?;
    let part: u32 = part.parse().map_err(|_| Failure(StatusCode::NOT_FOUND))?;
    let key = camera.clone(); let bytes = io(&c, move |s| s.clip_part(&key,&id,part)).await?;
    scoped(&c, &headers, &camera, false).await?;
    let mut response = Response::new(Body::from(bytes));
    response.headers_mut().insert(header::CONTENT_TYPE, "application/octet-stream".parse().unwrap());
    Ok(response)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Keep { kept: bool }
async fn keep(State(c): State<ContextRef>, Path((camera,id)): Path<(String,String)>, headers: HeaderMap,
              payload: Result<Json<Keep>, axum::extract::rejection::JsonRejection>) -> Result<Json<Value>, Failure> {
    scoped(&c, &headers, &camera, true).await?;
    let Json(payload) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    let event = io(&c, move |store| store.keep(&camera, &id, payload.kept)).await?;
    Ok(Json(json!({"api_version":"1", "event":event})))
}
async fn remove(State(c): State<ContextRef>, Path((camera,id)): Path<(String,String)>, headers: HeaderMap) -> Result<StatusCode, Failure> {
    scoped(&c, &headers, &camera, true).await?;
    io(&c, move |store| store.remove(&camera, &id)).await?;
    Ok(StatusCode::NO_CONTENT)
}
async fn delete_command(State(c): State<ContextRef>, Path((camera,id)): Path<(String,String)>, headers: HeaderMap,
    payload: Result<Json<crate::Empty>, axum::extract::rejection::JsonRejection>) -> Result<StatusCode, Failure> {
    scoped(&c, &headers, &camera, true).await?;
    let Json(_) = payload.map_err(|_| Failure(StatusCode::BAD_REQUEST))?;
    remove(State(c), Path((camera,id)), headers).await
}
pub(crate) fn routes(policy: Shared, store: Option<Store>) -> Router {
    Router::new().route("/v1/cameras/{camera}/activity", get(first))
        .route("/v1/cameras/{camera}/activity/before/{cursor}", get(next))
        .route("/v1/cameras/{camera}/activity/{id}/clip", get(clip))
        .route("/v1/cameras/{camera}/activity/{id}/clip/parts/{part}", get(clip_part))
        .route("/v1/cameras/{camera}/activity/{id}/thumbnail", get(thumbnail))
        .route("/v1/cameras/{camera}/activity/{id}/keep", post(keep))
        .route("/v1/cameras/{camera}/activity/{id}", delete(remove))
        .route("/v1/cameras/{camera}/activity/{id}/delete", post(delete_command))
        .with_state(Arc::new(Context { policy, store, io: Arc::new(Semaphore::new(4)) }))
}

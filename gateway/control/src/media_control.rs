//! Bounded loopback-only MediaMTX reconciliation. No general URL fetcher or media transport.
use crate::{security::{session_id, RemoteSession, Settings, HEALTH_WINDOW, MAX_SESSIONS}, Shared};
use axum::http::{header, Method, Request, StatusCode};
use bytes::Bytes;
use http_body_util::{BodyExt, Empty, Limited};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use std::{collections::HashSet, time::{Duration, Instant}};
use tokio::{net::TcpStream, task::JoinHandle};

#[derive(Deserialize)]
struct Listing {
    #[serde(rename = "itemCount")] count: usize,
    #[serde(rename = "pageCount")] pages: usize,
    items: Vec<RemoteSession>,
}
struct Driver(JoinHandle<()>);
impl Drop for Driver { fn drop(&mut self) { self.0.abort(); } }

async fn exchange(settings: &Settings, method: Method, path: &str) -> Result<(StatusCode, Vec<u8>), ()> {
    tokio::time::timeout(Duration::from_secs(2), async {
        let socket = TcpStream::connect(settings.media_api).await.map_err(|_| ())?;
        let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(socket)).await.map_err(|_| ())?;
        let _driver = Driver(tokio::spawn(async move { let _ = connection.await; }));
        let request = Request::builder().method(method).uri(path)
            .header(header::HOST, settings.media_api.to_string())
            .header(header::AUTHORIZATION, format!("Bearer {}", settings.media_api_token))
            .header(header::ACCEPT, "application/json")
            .header(header::CONNECTION, "close")
            .body(Empty::<Bytes>::new()).map_err(|_| ())?;
        let response = sender.send_request(request).await.map_err(|_| ())?;
        let status = response.status();
        // No redirects, decompression, cookies, proxy environment, DNS or alternate origins.
        if ![StatusCode::OK, StatusCode::NOT_FOUND].contains(&status) { return Err(()); }
        let content_type = response.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("");
        if content_type.split(';').next() != Some("application/json") { return Err(()); }
        let body = Limited::new(response.into_body(), 1_048_576).collect().await.map_err(|_| ())?.to_bytes();
        Ok((status, body.to_vec()))
    }).await.map_err(|_| ())?
}

pub struct Maintenance { shared: Shared, settings: Option<Settings> }
impl Maintenance {
    pub(crate) fn new(shared: Shared, settings: Option<Settings>) -> Self { Self { shared, settings } }
    async fn reconcile(&self, settings: &Settings) -> Result<(), ()> {
        let (status, bytes) = exchange(settings, Method::GET, "/v3/webrtcsessions/list?itemsPerPage=256").await?;
        if status != StatusCode::OK { return Err(()); }
        let listing: Listing = serde_json::from_slice(&bytes).map_err(|_| ())?;
        if listing.pages > 1 || listing.count != listing.items.len() || listing.items.len() > MAX_SESSIONS
            || listing.items.iter().any(|s| !session_id(&s.id) || !["read", "publish"].contains(&s.state.as_str()))
            || listing.items.iter().map(|s| &s.id).collect::<HashSet<_>>().len() != listing.items.len() {
            return Err(());
        }
        let denied = self.shared.lock().await.plan(&listing.items, Instant::now());
        for id in denied {
            // Recheck after the HTTP/list await: a fresh legitimate admission can race the snapshot.
            if !self.shared.lock().await.still_forbidden(&id) { continue; }
            exchange(settings, Method::POST, &format!("/v3/webrtcsessions/kick/{id}")).await?;
            self.shared.lock().await.retired(&id);
        }
        Ok(())
    }
    pub async fn run(self) {
        let Some(settings) = &self.settings else { std::future::pending::<()>().await; return; };
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut previous = None;
        // Startup and loss of management have the same finite health budget.
        // Only a complete list/kick reconciliation can extend it. Returning
        // retires the controller; the strict service group then closes media.
        let credential_deadline = self.shared.lock().await.credential_deadline();
        let mut deadline = Instant::now() + HEALTH_WINDOW;
        loop {
            if !self.shared.lock().await.credentials_ready() { return; }
            let activation_deadline = credential_deadline.map_or(deadline, |at| at.min(deadline));
            tokio::select! {
                biased;
                _ = tokio::time::sleep_until(activation_deadline.into()) => return,
                _ = timer.tick() => {},
            }
            let success = matches!(tokio::time::timeout_at(activation_deadline.into(),
                tokio::time::timeout(Duration::from_secs(4), self.reconcile(settings))).await,
                Ok(Ok(Ok(()))));
            let now = Instant::now();
            // A ready future must not renew health after the deadline merely
            // because its last bounded synchronous step completed in this poll.
            if now >= activation_deadline || !self.shared.lock().await.credentials_ready() {
                self.shared.lock().await.synchronized(false, now);
                return;
            }
            self.shared.lock().await.synchronized(success, now);
            if success { deadline = now + HEALTH_WINDOW; }
            if previous != Some(success) {
                eprintln!("nxrs-gateway media control {}", if success { "ready" } else { "unavailable; new viewing denied" });
                previous = Some(success);
            }
        }
    }
}

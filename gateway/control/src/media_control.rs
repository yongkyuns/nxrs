//! Bounded loopback-only MediaMTX reconciliation. No general URL fetcher or media transport.
use crate::{security::{session_id, RemoteSession, Settings, Transport, MAX_SESSIONS}, Shared};
use axum::http::{header, Method, Request, StatusCode};
use bytes::Bytes;
use http_body_util::{BodyExt, Empty, Limited};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use std::{collections::HashSet, time::{Duration, Instant}};
use tokio::{net::TcpStream, task::JoinHandle};

#[derive(Deserialize)]
struct Listing<T> {
    #[serde(rename = "itemCount")] count: usize,
    #[serde(rename = "pageCount")] pages: usize,
    items: Vec<T>,
}
#[derive(Deserialize)]
struct RtspSession { id: String, state: String, path: String }

impl Transport {
    fn api(self) -> &'static str {
        match self { Self::WebRtc => "webrtcsessions", Self::Rtsp => "rtspsessions" }
    }

    fn listing(self, bytes: &[u8]) -> Result<Listing<RemoteSession>, ()> {
        match self {
            Self::WebRtc => serde_json::from_slice(bytes).map_err(|_| ()),
            Self::Rtsp => {
                let listing: Listing<RtspSession> = serde_json::from_slice(bytes).map_err(|_| ())?;
                let items = listing.items.into_iter().map(|s| RemoteSession {
                    established: s.state == "read", id: s.id, state: s.state, path: s.path,
                }).collect();
                Ok(Listing { count: listing.count, pages: listing.pages, items })
            }
        }
    }
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
    /// End long-lived metadata bodies before the listeners begin draining.
    pub fn stop_detection_streams(&self) -> impl std::future::Future<Output = ()> + Send + 'static {
        let shared = self.shared.clone();
        async move { shared.lock().await.detections.shutting_down = true; }
    }
    pub(crate) fn new(shared: Shared, settings: Option<Settings>) -> Self { Self { shared, settings } }
    async fn reconcile(&self, settings: &Settings, transport: Transport) -> Result<(), ()> {
        let snapshot_started = Instant::now();
        let (status, bytes) = exchange(settings, Method::GET, &format!("/v3/{}/list?itemsPerPage=256", transport.api())).await?;
        if status != StatusCode::OK { return Err(()); }
        let listing = transport.listing(&bytes)?;
        if listing.pages > 1 || listing.count != listing.items.len() || listing.items.len() > MAX_SESSIONS
            || listing.items.iter().any(|s| !session_id(&s.id) || !(["read", "publish"].contains(&s.state.as_str())
                || transport == Transport::Rtsp && s.state == "idle"))
            || listing.items.iter().map(|s| &s.id).collect::<HashSet<_>>().len() != listing.items.len() {
            return Err(());
        }
        let denied = self.shared.lock().await.plan(transport, &listing.items, snapshot_started);
        for id in denied {
            // Recheck after the HTTP/list await: a fresh legitimate admission can race the snapshot.
            if !self.shared.lock().await.still_forbidden(transport, &id) { continue; }
            exchange(settings, Method::POST, &format!("/v3/{}/kick/{id}", transport.api())).await?;
            self.shared.lock().await.retired(transport, &id);
        }
        Ok(())
    }
    async fn supervise(&self, settings: &Settings, transport: Transport) {
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut previous = None;
        loop {
            timer.tick().await;
            let success = matches!(tokio::time::timeout(Duration::from_secs(4), self.reconcile(settings, transport)).await, Ok(Ok(())));
            self.shared.lock().await.synchronized(transport, success, Instant::now());
            if previous != Some(success) {
                let name = match transport { Transport::WebRtc => "media control", Transport::Rtsp => "RTSP source control" };
                eprintln!("nxrs-gateway {name} {}", if success { "ready" } else { "unavailable; new viewing denied" });
                previous = Some(success);
            }
        }
    }
    pub async fn run(self) {
        let Some(settings) = &self.settings else { std::future::pending::<()>().await; return; };
        tokio::join!(self.supervise(settings, Transport::WebRtc), self.supervise(settings, Transport::Rtsp));
    }
}

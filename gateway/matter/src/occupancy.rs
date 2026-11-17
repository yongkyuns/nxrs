//! Confirmed occupancy from the gateway's authenticated detection subscription.
use bytes::Bytes;
pub use detection_wire::Presence;
use detection_wire::{Snapshot, MAX_AGE_MS, SOURCE_TIMEOUT_MS};
use http_body_util::{BodyExt, Empty};
use hyper::{client::conn::http1, header, Request, StatusCode};
use hyper_util::rt::TokioIo;
use std::{io::Read, net::SocketAddr, path::Path, time::Duration};
use tokio::{
    net::TcpStream,
    time::{self, Instant},
};

const MAX_EVENT: usize = 8192;
const SILENCE: Duration = Duration::from_millis(2 * SOURCE_TIMEOUT_MS);

/// One camera on the local gateway. The owner runs this future on its executor.
pub struct Feed {
    control: SocketAddr,
    camera: String,
    authorization: header::HeaderValue,
}

impl Feed {
    pub fn open(
        control: SocketAddr,
        camera: &str,
        credential: &Path,
    ) -> Result<Self, &'static str> {
        if !control.ip().is_loopback() || control.port() == 0 {
            return Err("detection control requires a loopback endpoint");
        }
        if camera.is_empty()
            || camera.len() > 64
            || !camera
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        {
            return Err("invalid camera identifier");
        }
        let mut text = String::new();
        crate::platform::open_file(credential, false, false)
            .and_then(|file| file.take(66).read_to_string(&mut text))
            .map_err(|_| "unreadable detection credential")?;
        let token = text.strip_suffix('\n').unwrap_or(&text);
        if token.len() != 64 || !lower_hex(token) {
            return Err("invalid detection credential");
        }
        let mut authorization = header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| "invalid detection credential")?;
        authorization.set_sensitive(true);
        Ok(Self {
            control,
            camera: camera.into(),
            authorization,
        })
    }

    /// Emits the current level only. A disconnected, expired or malformed stream
    /// withdraws occupancy. Cancellation withdraws it and releases the socket.
    pub async fn run(&self, mut publish: impl FnMut(Presence)) {
        publish(Presence::Unavailable);
        let mut level = Level {
            state: Presence::Unavailable,
            publish,
        };
        loop {
            let _ = self.session(&mut |state| level.set(state)).await;
            level.set(Presence::Unavailable);
            time::sleep(Duration::from_secs(1)).await;
        }
    }

    async fn session(&self, publish: &mut impl FnMut(Presence)) -> Result<(), ()> {
        let started = Instant::now();
        let opening = started + SILENCE;
        let socket = time::timeout_at(opening, TcpStream::connect(self.control))
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
        let (mut sender, connection) = http1::Builder::new()
            .max_buf_size(16 * 1024)
            .handshake(TokioIo::new(socket))
            .await
            .map_err(|_| ())?;
        let request = Request::get(format!("/v1/cameras/{}/detections", self.camera))
            .header(header::HOST, self.control.to_string())
            .header(header::AUTHORIZATION, self.authorization.clone())
            .header(header::ACCEPT, "text/event-stream")
            .body(Empty::<Bytes>::new())
            .map_err(|_| ())?;
        let receive = async {
            let response = time::timeout_at(opening, sender.send_request(request))
                .await
                .map_err(|_| ())?
                .map_err(|_| ())?;
            if response.status() != StatusCode::OK
                || response
                    .headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|h| h.to_str().ok())
                    .and_then(|v| v.split(';').next())
                    != Some("text/event-stream")
            {
                return Err(());
            }
            let mut body = response.into_body();
            let mut event = Vec::new();
            let mut cursor = Cursor::default();
            let mut fresh_until = None;
            let mut reconnect = Instant::now() + SILENCE;
            loop {
                let deadline = fresh_until.map_or(reconnect, |t: Instant| t.min(reconnect));
                tokio::select! {
                    biased;
                    _ = time::sleep_until(deadline) => {
                        publish(Presence::Unavailable);
                        if Instant::now() >= reconnect { return Err(()); }
                        fresh_until = None;
                    }
                    frame = body.frame() => {
                        let data = frame.ok_or(())?.map_err(|_| ())?.into_data().map_err(|_| ())?;
                        for &byte in &data {
                            if event.len() == MAX_EVENT { return Err(()); }
                            event.push(byte);
                            if event.ends_with(b"\n\n") || event.ends_with(b"\r\n\r\n") {
                                let now = Instant::now();
                                let (state, remaining) = cursor.read(&event, &self.camera, now - started)?;
                                fresh_until = remaining.map(|duration| now + duration);
                                reconnect = now + SILENCE;
                                publish(state);
                                event.clear();
                            }
                        }
                    }
                }
            }
        };
        // Drive HTTP on the owning future. Closing the session also drops its I/O.
        tokio::select! { _ = connection => Err(()), result = receive => result }
    }
}

fn lower_hex(s: &str) -> bool {
    s.bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

struct Level<F: FnMut(Presence)> {
    state: Presence,
    publish: F,
}

impl<F: FnMut(Presence)> Level<F> {
    fn set(&mut self, state: Presence) {
        if self.state != state {
            self.state = state;
            (self.publish)(state);
        }
    }
}

impl<F: FnMut(Presence)> Drop for Level<F> {
    fn drop(&mut self) {
        self.set(Presence::Unavailable);
    }
}

#[derive(Default)]
struct Cursor {
    last: Option<(u64, u64, Option<String>)>,
}

impl Cursor {
    fn read(
        &mut self,
        event: &[u8],
        camera: &str,
        elapsed: Duration,
    ) -> Result<(Presence, Option<Duration>), ()> {
        let mut id = None;
        let mut data = None;
        let mut detection = false;
        for line in std::str::from_utf8(event)
            .map_err(|_| ())?
            .lines()
            .filter(|s| !s.is_empty())
        {
            let (key, value) = line.split_once(':').ok_or(())?;
            let value = value.strip_prefix(' ').unwrap_or(value);
            match key {
                "event" if !detection && value == "detection" => detection = true,
                "id" if id.is_none() => id = Some(value),
                "data" if data.is_none() => data = Some(value),
                _ => return Err(()),
            }
        }
        if !detection {
            return Err(());
        }
        let s: Snapshot = serde_json::from_str(data.ok_or(())?).map_err(|_| ())?;
        if s.camera_id != camera
            || !(1..=SOURCE_TIMEOUT_MS).contains(&s.valid_for_ms)
            || s.source_session_id
                .as_ref()
                .is_some_and(|v| v.len() != 32 || !lower_hex(v))
            || (s.source.is_some() && s.source_session_id.is_none())
        {
            return Err(());
        }
        if let Some(source) = &s.source {
            source.validate().map_err(|_| ())?;
        }
        let session = s.source_session_id.as_deref().unwrap_or("none");
        let events = s.source.as_ref().map_or(0, |source| source.event_count);
        if id != Some(&format!("{session}:{events}")) {
            return Err(());
        }
        if self.last.as_ref().is_some_and(|(rev, ms, source)| {
            s.revision < *rev
                || s.stream_elapsed_ms < *ms
                || (s.revision == *rev && source != &s.source_session_id)
        }) {
            return Err(());
        }
        self.last = Some((s.revision, s.stream_elapsed_ms, s.source_session_id));
        let server = Duration::from_millis(s.stream_elapsed_ms);
        if server > elapsed + Duration::from_millis(100) {
            return Err(());
        }
        if s.presence == Presence::Unavailable {
            return Ok((Presence::Unavailable, None));
        }
        let source = s.source.ok_or(())?;
        let age = s.observation_age_ms.ok_or(())?;
        if !s.source_online
            || s.presence != source.presence
            || age < source.emitted_ms - source.latest.ok_or(())?.captured_ms
            || age >= MAX_AGE_MS
        {
            return Err(());
        }
        let remaining = Duration::from_millis(s.valid_for_ms.min(MAX_AGE_MS - age))
            .saturating_sub(elapsed.saturating_sub(server));
        Ok(if remaining.is_zero() {
            (Presence::Unavailable, None)
        } else {
            (s.presence, Some(remaining))
        })
    }
}

#[cfg(test)]
mod tests;

//! Drive the per-camera recorder guard from the enrolled bridge's durable policy.
use super::{Error, Privacy};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{client::conn::http1, header, Method, Request, StatusCode};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use std::{
    net::SocketAddr,
    path::Path,
    sync::Mutex as SyncMutex,
    time::{Duration, Instant},
};
use tokio::{net::TcpStream, sync::Mutex, time};
use zeroize::Zeroizing;

const CONFIRMATION_LIFETIME: Duration = Duration::from_millis(1600);
const RENEWAL_INTERVAL: Duration = Duration::from_millis(700);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Deserialize)]
struct Status {
    enabled: bool,
}

pub struct Recording {
    control: SocketAddr,
    path: String,
    authorization: header::HeaderValue,
    write: Mutex<()>,
    confirmed: SyncMutex<Option<(bool, Instant)>>,
}

// An interrupted enable leaves background renewal closed, including when the
// Matter client cancels after the desired policy was written.
struct PendingEnable<'a, F: Fn(&[u8]) -> Result<(), Error>> {
    privacy: &'a Privacy,
    save: &'a F,
    committed: bool,
}

impl<F: Fn(&[u8]) -> Result<(), Error>> Drop for PendingEnable<'_, F> {
    fn drop(&mut self) {
        if !self.committed {
            let _ = self.privacy.set_recording(true, self.save);
        }
    }
}

impl Recording {
    pub(crate) fn open(
        control: SocketAddr,
        camera: &str,
        token: &Path,
    ) -> Result<Self, &'static str> {
        let token = Zeroizing::new(crate::credential::read(token)?);
        let mut authorization =
            header::HeaderValue::from_str(&format!("Bearer {}", token.as_str()))
                .map_err(|_| "invalid recording control credential")?;
        authorization.set_sensitive(true);
        Ok(Self {
            control,
            path: format!("/internal/cameras/{camera}/recording"),
            authorization,
            write: Mutex::new(()),
            confirmed: SyncMutex::new(None),
        })
    }

    pub fn read(&self, privacy: &Privacy) -> Result<bool, Error> {
        let expected = privacy.recording_private();
        self.confirmed
            .lock()
            .map_err(|_| Error::Unavailable)?
            .filter(|(private, at)| *private == expected && at.elapsed() < CONFIRMATION_LIFETIME)
            .map(|(private, _)| private)
            .ok_or(Error::Unavailable)
    }

    pub async fn set(
        &self,
        privacy: &Privacy,
        private: bool,
        save: impl Fn(&[u8]) -> Result<(), Error>,
    ) -> Result<(), Error> {
        let _guard = self.write.try_lock().map_err(|_| Error::Unavailable)?;
        self.confirm(None)?;
        if private {
            let saved = privacy.set_recording(true, &save);
            let stopped = self.apply(true).await;
            saved.and(stopped)
        } else {
            // Confirm this is a guarded recorder before persisting permission.
            self.status().await?;
            privacy.set_recording(false, &save)?;
            let mut enable = PendingEnable {
                privacy,
                save: &save,
                committed: false,
            };
            let result = self.apply(false).await;
            if result.is_ok() {
                enable.committed = true;
            }
            drop(enable);
            if result.is_err() {
                // An enable reply can be lost after the recorder received it.
                // Never renew it on failure and request rollback immediately.
                let _ = self.apply(true).await;
            }
            result
        }
    }

    /// Owns renewal on the bridge executor. The native recorder expires a
    /// lost permission even when the bridge cannot reach its shutdown path.
    pub async fn run(&self, privacy: &Privacy, commissioned: impl Fn() -> bool) {
        let mut renewed = time::interval(RENEWAL_INTERVAL);
        renewed.set_missed_tick_behavior(time::MissedTickBehavior::Delay);
        loop {
            renewed.tick().await;
            if let Ok(_guard) = self.write.try_lock() {
                let private = !commissioned() || privacy.recording_private();
                if self.apply(private).await.is_err() {
                    let _ = self.confirm(None);
                }
            }
        }
    }

    fn confirm(&self, value: Option<(bool, Instant)>) -> Result<(), Error> {
        *self.confirmed.lock().map_err(|_| Error::Unavailable)? = value;
        Ok(())
    }

    async fn apply(&self, private: bool) -> Result<(), Error> {
        let began = Instant::now();
        self.exchange(Some(!private)).await?;
        self.confirm(Some((private, began)))
    }

    async fn status(&self) -> Result<bool, Error> {
        let body = self.exchange(None).await?;
        let status: Status = serde_json::from_slice(&body).map_err(|_| Error::Unavailable)?;
        Ok(status.enabled)
    }

    async fn exchange(&self, enabled: Option<bool>) -> Result<Bytes, Error> {
        time::timeout(REQUEST_TIMEOUT, async {
            let socket = TcpStream::connect(self.control)
                .await
                .map_err(|_| Error::Unavailable)?;
            let (mut sender, connection) = http1::Builder::new()
                .max_buf_size(8192)
                .handshake(TokioIo::new(socket))
                .await
                .map_err(|_| Error::Unavailable)?;
            let mut request = Request::builder()
                .method(if enabled.is_some() {
                    Method::POST
                } else {
                    Method::GET
                })
                .uri(&self.path)
                .header(header::HOST, self.control.to_string())
                .header(header::AUTHORIZATION, self.authorization.clone())
                .header(header::CONNECTION, "close");
            let body = if let Some(enabled) = enabled {
                request = request.header(header::CONTENT_TYPE, "application/json");
                Bytes::from(serde_json::json!({"enabled": enabled}).to_string())
            } else {
                Bytes::new()
            };
            let request = request
                .body(Full::new(body))
                .map_err(|_| Error::Unavailable)?;
            let receive = async {
                let response = sender
                    .send_request(request)
                    .await
                    .map_err(|_| Error::Unavailable)?;
                if enabled.is_some() && response.status() == StatusCode::NO_CONTENT {
                    return Ok(Bytes::new());
                }
                if enabled.is_some()
                    || response.status() != StatusCode::OK
                    || response
                        .headers()
                        .get(header::CONTENT_TYPE)
                        .and_then(|value| value.to_str().ok())
                        .and_then(|value| value.split(';').next())
                        != Some("application/json")
                {
                    return Err(Error::Unavailable);
                }
                Ok(Limited::new(response.into_body(), 256)
                    .collect()
                    .await
                    .map_err(|_| Error::Unavailable)?
                    .to_bytes())
            };
            tokio::pin!(receive);
            tokio::select! {
                biased;
                result = &mut receive => result,
                closed = connection => {
                    // A complete response and a normal close can arrive together.
                    closed.map_err(|_| Error::Unavailable)?;
                    receive.await
                }
            }
        })
        .await
        .map_err(|_| Error::Unavailable)?
    }
}

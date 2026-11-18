//! Translate durable microphone privacy into a short-lived capture permit.
use super::{Privacy, Setting};
use bytes::Bytes;
use http_body_util::Full;
use hyper::{client::conn::http1, header, Request, StatusCode};
use hyper_util::rt::TokioIo;
use ring::rand::{SecureRandom, SystemRandom};
use std::{net::SocketAddr, path::Path, time::Duration};
use tokio::{
    net::TcpStream,
    time::{sleep, timeout},
};
use zeroize::Zeroizing;

pub struct Microphone {
    control: SocketAddr,
    path: String,
    authorization: header::HeaderValue,
}

impl Microphone {
    pub(crate) fn open(
        control: SocketAddr,
        camera: &str,
        token: &Path,
    ) -> Result<Self, &'static str> {
        let token = Zeroizing::new(crate::credential::read(token)?);
        let mut authorization =
            header::HeaderValue::from_str(&format!("Bearer {}", token.as_str()))
                .map_err(|_| "invalid microphone control credential")?;
        authorization.set_sensitive(true);
        Ok(Self {
            control,
            path: format!("/internal/cameras/{camera}/microphone"),
            authorization,
        })
    }

    /// The caller owns this future. Dropping it lets the permit expire even
    /// when the process cannot notify control during shutdown.
    pub async fn run(&self, privacy: &Privacy, commissioned: impl Fn() -> bool) {
        let mut state = privacy.0.subscribe();
        let entropy = SystemRandom::new();
        let mut current_epoch = None;
        let mut permit = None;
        loop {
            let policy = *state.borrow_and_update();
            if !commissioned()
                || policy.failed
                || policy.flags & Setting::MicrophoneMuted as u8 != 0
            {
                permit = None;
            } else if current_epoch != Some(policy.audio_epoch) || permit.is_none() {
                let mut id = [0; 16];
                permit = entropy.fill(&mut id).ok().map(|()| id);
            }
            current_epoch = Some(policy.audio_epoch);
            if timeout(Duration::from_millis(300), self.publish(permit)).await != Ok(Ok(())) {
                // Reconnection issues a new grant. Ingest will retire the
                // previous audio lifetime if it missed our withdrawal.
                permit = None;
            }
            sleep(Duration::from_millis(100)).await;
            tokio::select! {
                _ = state.changed() => {},
                _ = sleep(Duration::from_millis(300)) => {},
            }
        }
    }

    async fn publish(&self, permit: Option<[u8; 16]>) -> Result<(), ()> {
        let socket = TcpStream::connect(self.control).await.map_err(|_| ())?;
        let (mut sender, connection) = http1::Builder::new()
            .max_buf_size(8192)
            .handshake(TokioIo::new(socket))
            .await
            .map_err(|_| ())?;
        let request = Request::post(&self.path)
            .header(header::HOST, self.control.to_string())
            .header(header::AUTHORIZATION, self.authorization.clone())
            .header(header::CONNECTION, "close")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Full::new(Bytes::from(
                serde_json::json!({ "permit": permit }).to_string(),
            )))
            .map_err(|_| ())?;
        let send = async {
            let response = sender.send_request(request).await.map_err(|_| ())?;
            (response.status() == StatusCode::NO_CONTENT)
                .then_some(())
                .ok_or(())
        };
        tokio::select! { biased; result = send => result, _ = connection => Err(()) }
    }
}

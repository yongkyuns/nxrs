//! Apply per-camera microphone privacy through the authenticated capture session.
use super::{Error, Privacy, Setting};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{client::conn::http1, header, Request, StatusCode};
use hyper_util::rt::TokioIo;
use ring::rand::{SecureRandom, SystemRandom};
use std::{
    net::SocketAddr,
    path::Path,
    time::{Duration, Instant},
};
use tokio::{
    net::TcpStream,
    sync::{watch, Mutex},
    time::{sleep, timeout},
};
use zeroize::Zeroizing;

const RECEIPT_LIFETIME: Duration = Duration::from_millis(600);
const WRITE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Eq, PartialEq)]
struct Command {
    id: [u8; 16],
    muted: bool,
}

impl Command {
    fn new(muted: bool) -> Result<Self, Error> {
        let mut id = [0; 16];
        SystemRandom::new()
            .fill(&mut id)
            .map_err(|_| Error::Unavailable)?;
        Ok(Self { id, muted })
    }
}

pub struct Microphone {
    control: SocketAddr,
    path: String,
    authorization: header::HeaderValue,
    requested: watch::Sender<Option<Command>>,
    confirmed: watch::Sender<Option<(Command, Instant)>>,
    write: Mutex<()>,
}

struct PendingWrite<'a>(&'a watch::Sender<Option<Command>>);

impl Drop for PendingWrite<'_> {
    fn drop(&mut self) {
        self.0.send_replace(None);
    }
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
        Ok(Self::new(control, camera, authorization))
    }

    fn new(control: SocketAddr, camera: &str, authorization: header::HeaderValue) -> Self {
        Self {
            control,
            path: format!("/internal/cameras/{camera}/microphone"),
            authorization,
            requested: watch::Sender::new(None),
            confirmed: watch::Sender::new(None),
            write: Mutex::new(()),
        }
    }

    pub fn read(&self, privacy: &Privacy) -> Result<bool, Error> {
        let state = *privacy.0.borrow();
        let muted = state.flags & Setting::MicrophoneMuted as u8 != 0;
        if !state.failed
            && self.requested.borrow().is_none()
            && self.confirmed.borrow().is_some_and(|(command, time)| {
                command.muted == muted && time.elapsed() < RECEIPT_LIFETIME
            })
        {
            Ok(muted)
        } else {
            Err(Error::Unavailable)
        }
    }

    /// Mute closes local admission immediately. Unmute stays provisional until
    /// the capture session confirms it and the new setting can be saved.
    pub async fn set(
        &self,
        privacy: &Privacy,
        muted: bool,
        save: impl FnOnce(&[u8]) -> Result<(), Error>,
    ) -> Result<(), Error> {
        let _write = self.write.try_lock().map_err(|_| Error::Unavailable)?;
        if self.read(privacy) == Ok(muted) {
            return Ok(());
        }
        if muted {
            privacy.set(Setting::MicrophoneMuted, true, save)?;
            self.apply(Command::new(true)?).await?;
        } else {
            if privacy.0.borrow().failed {
                return Err(Error::Storage);
            }
            let _pending = self.apply(Command::new(false)?).await?;
            privacy.set(Setting::MicrophoneMuted, false, save)?;
        }
        Ok(())
    }

    async fn apply(&self, command: Command) -> Result<PendingWrite<'_>, Error> {
        let mut confirmed = self.confirmed.subscribe();
        self.requested.send_replace(Some(command));
        let pending = PendingWrite(&self.requested);
        timeout(WRITE_TIMEOUT, async {
            loop {
                if confirmed
                    .borrow_and_update()
                    .is_some_and(|(applied, time)| {
                        applied == command && time.elapsed() < RECEIPT_LIFETIME
                    })
                {
                    return Ok(pending);
                }
                confirmed.changed().await.map_err(|_| Error::Unavailable)?;
            }
        })
        .await
        .map_err(|_| Error::Unavailable)?
    }

    /// The caller owns this future. Dropping it lets the permit expire even
    /// when the process cannot notify control during shutdown.
    pub async fn run(&self, privacy: &Privacy, commissioned: impl Fn() -> bool) {
        let mut state = privacy.0.subscribe();
        let mut requested = self.requested.subscribe();
        let mut current: Option<Command> = None;
        loop {
            let policy = *state.borrow_and_update();
            let request = *requested.borrow_and_update();
            let active = commissioned() && !policy.failed;
            let muted = !active || policy.flags & Setting::MicrophoneMuted as u8 != 0;
            let explicit = request.filter(|_| active);
            current = explicit.or_else(|| {
                current
                    .filter(|command| command.muted == muted)
                    .or_else(|| Command::new(muted).ok())
            });
            let confirmation = if let Some(command) = current {
                // Start the freshness limit at request admission, including network time.
                let sent = Instant::now();
                // Allow the hardware to acknowledge provisional unmute, but release
                // PCM to the shared publication only after the setting is durable.
                let hold_audio = muted && !command.muted;
                match timeout(
                    Duration::from_millis(300),
                    self.publish(command, hold_audio),
                )
                .await
                {
                    Ok(Ok(true)) => Some((command, sent)),
                    Ok(Ok(false)) => None,
                    _ => {
                        // A renewed background grant after reconnection must not
                        // inherit audio from the previous capture lifetime.
                        current = None;
                        None
                    }
                }
            } else {
                None
            };
            self.confirmed.send_replace(confirmation);
            sleep(Duration::from_millis(100)).await;
            tokio::select! {
                _ = state.changed() => {},
                _ = requested.changed() => {},
                _ = sleep(Duration::from_millis(300)) => {},
            }
        }
    }

    async fn publish(&self, command: Command, hold_audio: bool) -> Result<bool, ()> {
        let permit = (!command.muted).then_some(command.id);
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
                serde_json::json!({
                    "permit": permit, "command_id": command.id, "hold_audio": hold_audio,
                })
                .to_string(),
            )))
            .map_err(|_| ())?;
        let send = async {
            let response = sender.send_request(request).await.map_err(|_| ())?;
            if response.status() != StatusCode::OK {
                return Err(());
            }
            let body = Limited::new(response.into_body(), 8)
                .collect()
                .await
                .map_err(|_| ())?;
            serde_json::from_slice(&body.to_bytes()).map_err(|_| ())
        };
        tokio::pin!(send);
        tokio::select! {
            biased;
            result = &mut send => result,
            closed = connection => {
                closed.map_err(|_| ())?;
                // A normal close can arrive in the same poll as the complete response.
                send.await
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{
        io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
        net::TcpListener,
    };

    #[tokio::test(start_paused = true)]
    async fn timeout_and_cancellation_preserve_mute_until_source_confirmation() {
        let microphone = Microphone::new(
            "127.0.0.1:1".parse().unwrap(),
            "test",
            header::HeaderValue::from_static("Bearer fixture"),
        );
        let privacy = Privacy::load(None).unwrap();
        let mut requested = microphone.requested.subscribe();
        {
            let change = microphone.set(&privacy, false, |_| panic!("saved before source ACK"));
            tokio::pin!(change);
            assert!(futures_util::poll!(&mut change).is_pending());
            assert!(!requested.borrow_and_update().unwrap().muted);
            assert!(privacy.get(Setting::MicrophoneMuted));
        }
        assert!(requested.borrow_and_update().is_none());
        assert_eq!(microphone.read(&privacy), Err(Error::Unavailable));

        let response = microphone.set(&privacy, false, |record| {
            assert_eq!(record, &[1, 0]);
            Ok(())
        });
        let acknowledge = async {
            requested.changed().await.unwrap();
            let command = requested.borrow_and_update().unwrap();
            microphone
                .confirmed
                .send_replace(Some((command, Instant::now())));
        };
        assert_eq!(tokio::join!(response, acknowledge).0, Ok(()));
        assert_eq!(microphone.read(&privacy), Ok(false));

        assert_eq!(
            microphone
                .set(&privacy, true, |record| {
                    assert_eq!(record, &[1, 1]);
                    Ok(())
                })
                .await,
            Err(Error::Unavailable),
        );
        assert!(privacy.get(Setting::MicrophoneMuted));
        assert!(requested.borrow().is_none());
        assert_eq!(microphone.read(&privacy), Err(Error::Unavailable));
    }

    #[tokio::test]
    async fn successful_reply_survives_http_connection_close() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let microphone = Microphone::new(
            listener.local_addr().unwrap(),
            "test",
            header::HeaderValue::from_static("Bearer fixture"),
        );
        let command = Command {
            id: [0; 16],
            muted: true,
        };
        let server = async {
            let (socket, _) = listener.accept().await.unwrap();
            let mut socket = BufReader::new(socket);
            loop {
                let mut line = String::new();
                assert_ne!(socket.read_line(&mut line).await.unwrap(), 0);
                if line == "\r\n" {
                    break;
                }
            }
            let expected = serde_json::json!({ "command_id": command.id, "hold_audio": false, "permit": null });
            let mut body = vec![0; expected.to_string().len()];
            socket.read_exact(&mut body).await.unwrap();
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
                expected
            );
            let socket = socket.get_mut();
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\ntrue")
                .await
                .unwrap();
            socket.shutdown().await.unwrap();
        };
        let ((), result) = timeout(Duration::from_secs(2), async {
            tokio::join!(server, microphone.publish(command, false))
        })
        .await
        .unwrap();
        assert_eq!(result, Ok(true));
    }
}

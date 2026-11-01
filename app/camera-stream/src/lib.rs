//! Portable camera-to-viewer composition. The platform supplies device, storage,
//! listener and time; connectivity and scheduling stay in the owner's event loop.
#![forbid(unsafe_code)]

mod detection;
pub mod bootstrap;
pub mod network;
mod publication;
pub mod startup;
mod tls;
pub mod update;
pub use detection::{DetectionSnapshot, SecurityDetection, SecurityEvent};
pub use tls::{TlsIdentity, TlsOptions, TlsStats, Worker as TlsOutput};

use camera::{Camera, Format, PixelFormat};
use camera_service::{CameraService, CaptureProgress, Frames};
use std::io;
use std::net::TcpListener;
use stream_service::{Options, Progress, Server, ServerStats};

#[derive(Debug)]
pub enum Error {
    Camera(camera_service::Error),
    Network(io::Error),
    Detection(detection_service::Error),
    Tls(::tls::Error),
    Worker,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Stats {
    pub captured: u64,
    pub captured_bytes: u64,
    pub max_frame_bytes: usize,
    pub camera_discarded: Option<u64>,
    pub stream: ServerStats,
    pub tls: Option<TlsStats>,
}

/// One history frame, one capture staging frame, and one in-flight network
/// frame. Plaintext uses three injected buffers and no thread. TLS adds one
/// injected handoff buffer and one worker. Detection has its own optional worker.
pub struct LiveCamera<D, const BYTES: usize, B, O = Server<B>> {
    // Join TLS before releasing camera storage, including implicit teardown.
    server: O,
    capture: CameraService<D, BYTES, 1, B>,
    stats: Stats,
    detection: Option<SecurityDetection>,
}

/// The camera owns one output with explicit polling and retirement. Implementations
/// must not retain borrowed capture data after `poll` returns.
pub trait StreamOutput {
    fn poll(
        &mut self,
        now: u64,
        online: bool,
        frame: Option<camera::Frame<'_>>,
        metadata: Option<stream_service::Metadata<'_>>,
    ) -> Result<Progress, Error>;
    fn stop(&mut self) -> Result<(), Error>;
    fn stats(&self) -> (ServerStats, Option<TlsStats>);
    fn stopped(&self) -> bool;
}
impl<B: AsRef<[u8]> + AsMut<[u8]>> StreamOutput for Server<B> {
    fn poll(
        &mut self,
        now: u64,
        online: bool,
        frame: Option<camera::Frame<'_>>,
        metadata: Option<stream_service::Metadata<'_>>,
    ) -> Result<Progress, Error> {
        if online {
            self.poll_with_metadata(now, frame, metadata)
                .map_err(Error::Network)
        } else {
            self.disconnect();
            Ok(Progress::default())
        }
    }
    fn stop(&mut self) -> Result<(), Error> {
        self.disconnect();
        Ok(())
    }
    fn stats(&self) -> (ServerStats, Option<TlsStats>) {
        (Server::stats(self), None)
    }
    fn stopped(&self) -> bool {
        false
    }
}
impl<B: AsRef<[u8]> + AsMut<[u8]>> StreamOutput for TlsOutput<B> {
    fn poll(
        &mut self,
        now: u64,
        online: bool,
        frame: Option<camera::Frame<'_>>,
        metadata: Option<stream_service::Metadata<'_>>,
    ) -> Result<Progress, Error> {
        self.poll(now, online, frame, metadata)
    }
    fn stop(&mut self) -> Result<(), Error> {
        self.stop()
    }
    fn stats(&self) -> (ServerStats, Option<TlsStats>) {
        let (stream, tls) = self.stats();
        (stream, Some(tls))
    }
    fn stopped(&self) -> bool {
        self.stopped()
    }
}

impl<D: Camera, const BYTES: usize, B: AsRef<[u8]> + AsMut<[u8]>> LiveCamera<D, BYTES, B> {
    pub fn new(
        device: D,
        listener: TcpListener,
        history: B,
        staging: B,
        outgoing: B,
        options: Options,
    ) -> Result<Self, Error> {
        let capture =
            CameraService::with_buffers(device, [history], staging).map_err(Error::Camera)?;
        let server = Server::new(listener, outgoing, options).map_err(Error::Network)?;
        Ok(Self {
            capture,
            server,
            stats: Stats::default(),
            detection: None,
        })
    }

    /// TLS-only output. Buffers are history, capture staging, network output
    /// and one owned handoff. Initialize identity/enrollment on its worker;
    /// the non-Send key never leaves that thread. There is no plaintext fallback.
    pub fn new_tls<T: FnMut(&::tls::Identity) -> Result<::tls::TimeLease, ::tls::Error> + 'static>(
        device: D,
        listener: TcpListener,
        [history, staging, outgoing, handoff]: [B; 4],
        options: Options,
        resources: TlsOptions,
        initialize: impl FnOnce() -> Result<TlsIdentity<T>, Error> + Send + 'static,
    ) -> Result<LiveCamera<D, BYTES, B, TlsOutput<B>>, Error>
    where
        B: Send + 'static,
    {
        let capture =
            CameraService::with_buffers(device, [history], staging).map_err(Error::Camera)?;
        let server =
            tls::Worker::spawn(listener, outgoing, handoff, options, resources, initialize)?;
        Ok(LiveCamera {
            capture,
            server,
            stats: Stats::default(),
            detection: None,
        })
    }
}

impl<D: Camera, const BYTES: usize, B: AsRef<[u8]> + AsMut<[u8]>>
    LiveCamera<D, BYTES, B, TlsOutput<B>>
{
    /// Configure before polling. Only the approved mutual-TLS peer can upload.
    pub fn enable_updates(&mut self, update: update::Update) -> Result<(), Error> {
        self.server.enable_updates(update)
    }
}

impl<D: Camera, const BYTES: usize, B: AsRef<[u8]> + AsMut<[u8]>, O: StreamOutput>
    LiveCamera<D, BYTES, B, O>
{
    /// Attach opt-in inference before capture starts. The product supplies an
    /// initialized worker; inference neither takes camera ownership nor
    /// competes for the single video client. Results stay in typed Rust state.
    pub fn attach_detection(&mut self, detection: SecurityDetection) -> Result<(), Error> {
        if self.capture.state() != camera_service::CameraState::Stopped {
            return Err(Error::Camera(camera_service::Error::AlreadyRunning));
        }
        self.detection = Some(detection);
        Ok(())
    }

    pub fn detection(&self) -> Option<&SecurityDetection> {
        self.detection.as_ref()
    }

    pub fn start(&mut self, width: u16, height: u16) -> Result<Format, Error> {
        if self.server.stopped() {
            return Err(Error::Worker);
        }
        if self
            .detection
            .as_ref()
            .is_some_and(SecurityDetection::stopped)
        {
            return Err(Error::Detection(detection_service::Error::Stopped));
        }
        let actual = self
            .capture
            .start(Format {
                width,
                height,
                pixels: PixelFormat::Jpeg,
            })
            .map_err(Error::Camera)?;
        if actual.pixels != PixelFormat::Jpeg {
            // Negotiation is allowed by Camera, but this transport does not
            // encode raw pixels. Stop before accepting an unusable stream.
            self.capture.stop().map_err(Error::Camera)?;
            return Err(Error::Camera(camera_service::Error::InvalidFormat));
        }
        Ok(actual)
    }

    /// Capture proceeds without a viewer and while offline. Slow clients never
    /// hold the capture buffer; the network service skips to the latest frame.
    pub fn poll(&mut self, now_ms: u64, online: bool) -> Result<Progress, Error> {
        let captured = match self.capture.poll(now_ms) {
            Ok(progress) => progress,
            Err(error) => {
                // An acquisition/clock failure must also retire autonomous TLS
                // output. This closes admission without joining on capture.
                let _ = self.server.poll(now_ms, false, None, None);
                return Err(Error::Camera(error));
            }
        };
        if let CaptureProgress::Published(_) = captured {
            self.stats.captured = self.stats.captured.saturating_add(1);
            if let Some(frame) = self
                .capture
                .next_after(self.capture.latest_sequence().saturating_sub(1))
            {
                self.stats.captured_bytes = self
                    .stats
                    .captured_bytes
                    .saturating_add(frame.bytes.len() as u64);
                self.stats.max_frame_bytes = self.stats.max_frame_bytes.max(frame.bytes.len());
            }
        }
        if let Some(detection) = self.detection.as_mut() {
            let latest = self
                .capture
                .next_after(self.capture.latest_sequence().saturating_sub(1));
            detection.poll(now_ms, latest);
        }
        let latest = self
            .capture
            .next_after(self.capture.latest_sequence().saturating_sub(1));
        let metadata = self
            .detection
            .as_ref()
            .and_then(SecurityDetection::metadata);
        self.server.poll(now_ms, online, latest, metadata)
    }

    pub fn stats(&self) -> Stats {
        let (stream, tls) = self.server.stats();
        Stats {
            stream,
            tls,
            camera_discarded: self.capture.backend().discarded_frames(),
            ..self.stats
        }
    }

    pub fn next_poll_at_ms(&self) -> Option<u64> {
        self.capture.next_poll_at_ms()
    }

    pub fn stop(&mut self) -> Result<(), Error> {
        let network = self.server.stop();
        let camera = self.capture.stop().map_err(Error::Camera);
        let detection = self
            .detection
            .as_mut()
            .map(SecurityDetection::stop)
            .unwrap_or(Ok(()))
            .map_err(Error::Detection);
        camera.and(detection).and(network)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, rc::Rc};

    #[test]
    fn capture_failure_retires_output_without_waiting_for_shutdown() {
        struct Failed;
        impl Camera for Failed {
            fn start(&mut self, f: Format) -> Result<Format, camera::DeviceError> {
                Ok(f)
            }
            fn poll_frame(
                &mut self,
                _: u64,
                _: &mut [u8],
            ) -> Result<Option<camera::Capture>, camera::DeviceError> {
                Err(camera::DeviceError::Io)
            }
            fn stop(&mut self) -> Result<(), camera::DeviceError> {
                Ok(())
            }
        }
        struct Output(Rc<Cell<bool>>);
        impl StreamOutput for Output {
            fn poll(
                &mut self,
                _: u64,
                online: bool,
                _: Option<camera::Frame<'_>>,
                _: Option<stream_service::Metadata<'_>>,
            ) -> Result<Progress, Error> {
                self.0.set(!online);
                Ok(Progress::default())
            }
            fn stop(&mut self) -> Result<(), Error> {
                panic!("capture must not join a worker");
            }
            fn stats(&self) -> (ServerStats, Option<TlsStats>) {
                Default::default()
            }
            fn stopped(&self) -> bool {
                false
            }
        }
        let retired = Rc::new(Cell::new(false));
        let mut live = LiveCamera {
            server: Output(retired.clone()),
            capture: CameraService::<_, 4, 1, _>::with_buffers(Failed, [[0; 4]], [0; 4]).unwrap(),
            stats: Stats::default(),
            detection: None,
        };
        live.start(1, 1).unwrap();
        assert!(matches!(live.poll(0, true), Err(Error::Camera(_))));
        assert!(retired.get());
    }
}

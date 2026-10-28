//! Portable camera-to-viewer composition. The platform supplies device, storage,
//! listener and time; connectivity and scheduling stay in the owner's event loop.
#![forbid(unsafe_code)]

mod detection;
mod publication;
pub use detection::{DetectionSnapshot, SecurityDetection, SecurityEvent};

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
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Stats {
    pub captured: u64,
    pub captured_bytes: u64,
    pub max_frame_bytes: usize,
    pub camera_discarded: Option<u64>,
    pub stream: ServerStats,
}

/// One history frame, one capture staging frame, and one in-flight network
/// frame. All three buffers are injected, allocated once and reused. This adds
/// no thread by default. Optional detection adds one explicitly supplied worker.
pub struct LiveCamera<D, const BYTES: usize, B> {
    capture: CameraService<D, BYTES, 1, B>,
    server: Server<B>,
    stats: Stats,
    detection: Option<SecurityDetection>,
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
        if let CaptureProgress::Published(_) = self.capture.poll(now_ms).map_err(Error::Camera)? {
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
        if !online {
            self.server.disconnect();
            return Ok(Progress::default());
        }
        let latest = self
            .capture
            .next_after(self.capture.latest_sequence().saturating_sub(1));
        let metadata = self
            .detection
            .as_ref()
            .and_then(SecurityDetection::metadata);
        self.server
            .poll_with_metadata(now_ms, latest, metadata)
            .map_err(Error::Network)
    }

    pub fn stats(&self) -> Stats {
        Stats {
            stream: self.server.stats(),
            camera_discarded: self.capture.backend().discarded_frames(),
            ..self.stats
        }
    }

    pub fn next_poll_at_ms(&self) -> Option<u64> {
        self.capture.next_poll_at_ms()
    }

    pub fn stop(&mut self) -> Result<(), Error> {
        self.server.disconnect();
        let camera = self.capture.stop().map_err(Error::Camera);
        let detection = self
            .detection
            .as_mut()
            .map(SecurityDetection::stop)
            .unwrap_or(Ok(()))
            .map_err(Error::Detection);
        camera.and(detection)
    }
}

/// Capture remains on its owner thread; the injected worker owns network/crypto.
/// History, staging, shared handoff and outgoing buffers are separately supplied.
/// No native key or callback context crosses threads.
pub struct ThreadedCamera<D, const BYTES: usize, B, W> {
    capture: CameraService<D, BYTES, 1, B>,
    worker: Option<stream_service::StreamWorker<W>>,
    stats: ThreadedStats,
    detection: Option<SecurityDetection>,
}
#[derive(Clone, Copy, Debug, Default)]
pub struct ThreadedStats {
    pub capture: Stats,
    pub handed_off: u64,
    pub skipped_busy: u64,
    pub rejected: u64,
    pub network_closed: u64,
}
impl<D, const BYTES: usize, B, W> ThreadedCamera<D, BYTES, B, W>
where
    D: Camera,
    B: AsRef<[u8]> + AsMut<[u8]>,
    W: AsRef<[u8]> + AsMut<[u8]> + Send + 'static,
{
    pub fn new(
        device: D,
        history: B,
        staging: B,
        worker: stream_service::StreamWorker<W>,
    ) -> Result<Self, Error> {
        let capture =
            CameraService::with_buffers(device, [history], staging).map_err(Error::Camera)?;
        Ok(Self {
            capture,
            worker: Some(worker),
            stats: ThreadedStats::default(),
            detection: None,
        })
    }
    /// Attach detection before capture starts. The network worker must have
    /// separately supplied storage for a complete bounded wire publication.
    pub fn attach_detection(&mut self, detection: SecurityDetection) -> Result<(), Error> {
        if self.capture.state() != camera_service::CameraState::Stopped {
            return Err(Error::Camera(camera_service::Error::AlreadyRunning));
        }
        if self
            .worker
            .as_ref()
            .is_none_or(|worker| worker.metadata_capacity() < detection_wire::MAX_SOURCE_BYTES)
        {
            return Err(Error::Detection(detection_service::Error::Configuration));
        }
        self.detection = Some(detection);
        Ok(())
    }
    pub fn detection(&self) -> Option<&SecurityDetection> {
        self.detection.as_ref()
    }
    pub fn start(&mut self, width: u16, height: u16) -> Result<Format, Error> {
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
            self.capture.stop().map_err(Error::Camera)?;
            return Err(Error::Camera(camera_service::Error::InvalidFormat));
        }
        Ok(actual)
    }
    /// Use the same monotonic epoch as WorkerReceiver::serve. Capture runs while
    /// offline or while the worker is busy/closed. Publishing uses try_lock;
    /// native cryptography never runs in this call. Counts distinguish capture,
    /// handoff, overload and the last observed asynchronous network counters.
    pub fn poll(
        &mut self,
        now_ms: u64,
        online: bool,
    ) -> Result<Option<stream_service::FramePublish>, Error> {
        use stream_service::FramePublish;
        if let Some(worker) = &self.worker {
            worker.set_online(online);
        }
        let result = match self.capture.poll(now_ms) {
            Ok(result) => result,
            Err(error) => {
                if let Some(worker) = &self.worker {
                    worker.set_online(false);
                }
                return Err(Error::Camera(error));
            }
        };
        if let Some(detection) = self.detection.as_mut() {
            let latest = self
                .capture
                .next_after(self.capture.latest_sequence().saturating_sub(1));
            detection.poll(now_ms, latest);
        }
        let mut publication = None;
        if let CaptureProgress::Published(sequence) = result {
            self.stats.capture.captured = self.stats.capture.captured.saturating_add(1);
            if let Some(frame) = self.capture.next_after(sequence.saturating_sub(1)) {
                self.stats.capture.captured_bytes = self
                    .stats
                    .capture
                    .captured_bytes
                    .saturating_add(frame.bytes.len() as u64);
                self.stats.capture.max_frame_bytes =
                    self.stats.capture.max_frame_bytes.max(frame.bytes.len());
                let handoff = self.worker.as_mut().map_or(FramePublish::Closed, |worker| {
                    worker.publish_with_metadata(
                        frame,
                        self.detection
                            .as_ref()
                            .and_then(SecurityDetection::metadata),
                    )
                });
                match handoff {
                    FramePublish::Published => {
                        self.stats.handed_off = self.stats.handed_off.saturating_add(1)
                    }
                    FramePublish::Busy => {
                        self.stats.skipped_busy = self.stats.skipped_busy.saturating_add(1)
                    }
                    FramePublish::Rejected(_) => {
                        self.stats.rejected = self.stats.rejected.saturating_add(1)
                    }
                    FramePublish::Closed => {
                        self.stats.network_closed = self.stats.network_closed.saturating_add(1)
                    }
                }
                publication = Some(handoff);
            }
        }
        if let Some(stats) = self.worker.as_ref().and_then(|worker| worker.stats()) {
            self.stats.capture.stream = stats;
        }
        self.stats.capture.camera_discarded = self.capture.backend().discarded_frames();
        Ok(publication)
    }
    /// Last observed worker counters may lag while it holds the exchange.
    pub fn stats(&self) -> ThreadedStats {
        self.stats
    }
    pub fn next_poll_at_ms(&self) -> Option<u64> {
        self.capture.next_poll_at_ms()
    }
    /// Request network stop and stop capture with its existing retryable
    /// cleanup contract. This does not join the network worker on the caller.
    pub fn stop(&mut self) -> Result<(), Error> {
        if let Some(worker) = &self.worker {
            worker.request_stop();
        }
        let camera = self.capture.stop().map_err(Error::Camera);
        let detection = self
            .detection
            .as_mut()
            .map(SecurityDetection::stop)
            .unwrap_or(Ok(()))
            .map_err(Error::Detection);
        camera.and(detection)
    }
    /// Explicit network retirement barrier, separate from capture polling and
    /// retryable camera stop. Owned SDK/key resources retire before it returns.
    pub fn finish_worker(&mut self) -> Result<(), Error> {
        if let Some(worker) = self.worker.take() {
            self.stats.capture.stream = worker.finish().map_err(Error::Network)?;
        }
        Ok(())
    }
}

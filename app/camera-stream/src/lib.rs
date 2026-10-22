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

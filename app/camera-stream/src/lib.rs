//! Portable camera-to-viewer composition. The platform supplies device, storage,
//! listener and time; connectivity and scheduling stay in the owner's event loop.
#![forbid(unsafe_code)]

use camera::{Camera, Format, PixelFormat};
use camera_service::{CameraService, CaptureProgress, Frames};
use std::io;
use std::net::TcpListener;
use stream_service::{Options, Progress, Server, ServerStats};

#[derive(Debug)]
pub enum Error {
    Camera(camera_service::Error),
    Network(io::Error),
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Stats {
    pub captured: u64,
    pub captured_bytes: u64,
    pub max_frame_bytes: usize,
    pub stream: ServerStats,
}

/// One history frame, one capture staging frame, and one in-flight network
/// frame. All three buffers are injected, allocated once and reused. This adds
/// no thread and accepts no hardware-specific types or feature flags.
pub struct LiveCamera<D, const BYTES: usize, B> {
    capture: CameraService<D, BYTES, 1, B>,
    server: Server<B>,
    stats: Stats,
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
        })
    }

    pub fn start(&mut self, width: u16, height: u16) -> Result<Format, Error> {
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
        if !online {
            self.server.disconnect();
            return Ok(Progress::default());
        }
        let latest = self
            .capture
            .next_after(self.capture.latest_sequence().saturating_sub(1));
        self.server.poll(now_ms, latest).map_err(Error::Network)
    }

    pub fn stats(&self) -> Stats {
        Stats {
            stream: self.server.stats(),
            ..self.stats
        }
    }

    pub fn next_poll_at_ms(&self) -> Option<u64> {
        self.capture.next_poll_at_ms()
    }

    pub fn stop(&mut self) -> Result<(), Error> {
        self.server.disconnect();
        self.capture.stop().map_err(Error::Camera)
    }
}

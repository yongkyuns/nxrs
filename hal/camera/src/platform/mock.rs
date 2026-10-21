//! Explicit scripted camera for host/WASM tests, not physical-device support.
#![forbid(unsafe_code)]
use crate::{Camera, Capture, DeviceError, Format, PixelFormat};
use alloc::collections::VecDeque;

pub const FORMAT: Format = Format {
    width: 2,
    height: 2,
    pixels: PixelFormat::Gray8,
};

#[derive(Clone, Copy, Debug)]
pub enum CameraAction {
    Frame(u8),
    Pending,
    Fail(DeviceError),
    Malformed(Capture),
}

pub struct MockCamera {
    pub actions: VecDeque<CameraAction>,
    pub negotiated: Option<Format>,
    pub start_failures: usize,
    pub stop_failures: usize,
    pub starts: usize,
    pub stops: usize,
    pub polls: usize,
    pub active: bool,
    actual: Format,
}

impl MockCamera {
    pub fn new(actions: impl IntoIterator<Item = CameraAction>) -> Self {
        Self {
            actions: actions.into_iter().collect(),
            negotiated: None,
            start_failures: 0,
            stop_failures: 0,
            starts: 0,
            stops: 0,
            polls: 0,
            active: false,
            actual: FORMAT,
        }
    }
}

impl Camera for MockCamera {
    fn start(&mut self, requested: Format) -> Result<Format, DeviceError> {
        self.starts += 1;
        if self.active {
            return Err(DeviceError::Busy);
        }
        if self.start_failures > 0 {
            self.start_failures -= 1;
            return Err(DeviceError::Io);
        }
        self.actual = self.negotiated.unwrap_or(requested);
        self.active = true;
        Ok(self.actual)
    }

    fn poll_frame(
        &mut self,
        now_ms: u64,
        bytes: &mut [u8],
    ) -> Result<Option<Capture>, DeviceError> {
        self.polls += 1;
        if !self.active {
            return Err(DeviceError::Io);
        }
        // Intentionally damage staging even when capture fails or is pending.
        bytes.fill(0xee);
        match self.actions.pop_front().unwrap_or(CameraAction::Pending) {
            CameraAction::Frame(value) => {
                let len = self.actual.raw_len().unwrap_or(4);
                let written = len.min(bytes.len());
                bytes[..written].fill(value);
                Ok(Some(Capture {
                    format: self.actual,
                    len,
                    timestamp_ms: now_ms,
                }))
            }
            CameraAction::Pending => Ok(None),
            CameraAction::Fail(error) => Err(error),
            CameraAction::Malformed(capture) => Ok(Some(capture)),
        }
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        self.stops += 1;
        if self.stop_failures > 0 {
            self.stop_failures -= 1;
            return Err(DeviceError::Busy);
        }
        self.active = false;
        Ok(())
    }
}

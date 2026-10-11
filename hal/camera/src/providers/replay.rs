use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::vec::Vec;

use crate::{Camera, Capture, DeviceError, Format};

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

/// Packed raw-frame replay. Loading is blocking setup; polling is a bounded
/// in-memory copy. Supports Gray8 and packed RGB565, not compressed JPEG,
/// row padding, live capture, or arbitrary video-container decoding.
pub struct ReplayCamera {
    bytes: Vec<u8>,
    format: Format,
    frame_len: usize,
    frame_count: usize,
    period_ms: u64,
    cursor: usize,
    epoch_ms: Option<u64>,
    last_poll_ms: Option<u64>,
    active: bool,
}

impl ReplayCamera {
    pub fn load(
        path: impl AsRef<Path>,
        format: Format,
        period_ms: u64,
        max_source_bytes: usize,
    ) -> io::Result<Self> {
        let frame_len = format
            .raw_len()
            .ok_or_else(|| invalid("replay requires a raw format"))?;
        if frame_len == 0 || !format.fits(max_source_bytes) || period_ms == 0 {
            return Err(invalid("invalid replay dimensions, period, or input limit"));
        }
        let limit = max_source_bytes
            .checked_add(1)
            .ok_or_else(|| invalid("input limit overflow"))?;
        let limit = u64::try_from(limit).map_err(|_| invalid("input limit overflow"))?;
        let mut bytes = Vec::new();
        File::open(path)?.take(limit).read_to_end(&mut bytes)?;
        if bytes.is_empty() || bytes.len() > max_source_bytes || bytes.len() % frame_len != 0 {
            return Err(invalid(
                "replay is empty, oversized, or ends with a partial frame",
            ));
        }
        let frame_count = bytes.len() / frame_len;
        u64::try_from(frame_count - 1)
            .ok()
            .and_then(|count| count.checked_mul(period_ms))
            .ok_or_else(|| invalid("replay duration overflow"))?;
        Ok(Self {
            bytes,
            format,
            frame_len,
            frame_count,
            period_ms,
            cursor: 0,
            epoch_ms: None,
            last_poll_ms: None,
            active: false,
        })
    }

    pub fn frame_count(&self) -> usize {
        self.frame_count
    }

    pub fn exhausted(&self) -> bool {
        self.cursor == self.frame_count
    }

    fn next_due_ms(&self) -> Option<u64> {
        if !self.active || self.exhausted() {
            return None;
        }
        let epoch = self.epoch_ms?;
        Some(epoch.saturating_add((self.cursor as u64).saturating_mul(self.period_ms)))
    }
}

impl Camera for ReplayCamera {
    fn start(&mut self, requested: Format) -> Result<Format, DeviceError> {
        if self.active {
            return Err(DeviceError::Busy);
        }
        if requested != self.format {
            return Err(DeviceError::Unsupported);
        }
        self.cursor = 0;
        self.epoch_ms = None;
        self.last_poll_ms = None;
        self.active = true;
        Ok(self.format)
    }

    fn poll_frame(
        &mut self,
        now_ms: u64,
        destination: &mut [u8],
    ) -> Result<Option<Capture>, DeviceError> {
        if !self.active {
            return Err(DeviceError::Io);
        }
        if self.last_poll_ms.is_some_and(|previous| now_ms < previous) {
            return Err(DeviceError::InvalidData);
        }
        self.last_poll_ms = Some(now_ms);
        if self.exhausted() {
            return Ok(None);
        }
        if destination.len() < self.frame_len {
            return Err(DeviceError::Full);
        }
        let epoch = *self.epoch_ms.get_or_insert(now_ms);
        let due = (self.cursor as u64)
            .checked_mul(self.period_ms)
            .and_then(|offset| epoch.checked_add(offset))
            .ok_or(DeviceError::InvalidData)?;
        if now_ms < due {
            return Ok(None);
        }
        let offset = self.cursor * self.frame_len;
        destination[..self.frame_len].copy_from_slice(&self.bytes[offset..offset + self.frame_len]);
        self.cursor += 1;
        Ok(Some(Capture {
            format: self.format,
            len: self.frame_len,
            timestamp_ms: due,
        }))
    }

    fn next_poll_at_ms(&self) -> Option<u64> {
        self.next_due_ms()
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        self.active = false;
        Ok(())
    }
}

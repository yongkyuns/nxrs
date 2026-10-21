//! Camera data and device contract; no implementation dependencies.
#![forbid(unsafe_code)]

pub use hal_common::DeviceError;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PixelFormat {
    Gray8,
    /// Packed RGB565, least-significant byte first, with no row padding.
    Rgb565,
    Jpeg,
}

/// Normalized JPEG quality: 1 is lowest quality, 100 is highest.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct JpegQuality(u8);

impl JpegQuality {
    pub const fn new(value: u8) -> Result<Self, DeviceError> {
        if value >= 1 && value <= 100 {
            Ok(Self(value))
        } else {
            Err(DeviceError::InvalidData)
        }
    }

    pub const fn get(self) -> u8 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Format {
    pub width: u16,
    pub height: u16,
    pub pixels: PixelFormat,
}

impl Format {
    /// Raw formats have exact lengths. JPEG is variable-length.
    pub fn raw_len(self) -> Option<usize> {
        let pixels = usize::from(self.width).checked_mul(usize::from(self.height))?;
        match self.pixels {
            PixelFormat::Gray8 => Some(pixels),
            PixelFormat::Rgb565 => pixels.checked_mul(2),
            PixelFormat::Jpeg => None,
        }
    }

    pub fn fits(self, capacity: usize) -> bool {
        if self.width == 0 || self.height == 0 || capacity == 0 {
            return false;
        }
        match self.pixels {
            PixelFormat::Jpeg => true,
            _ => self.raw_len().is_some_and(|len| len <= capacity),
        }
    }

    pub fn accepts_len(self, len: usize) -> bool {
        self.width != 0
            && self.height != 0
            && match self.pixels {
                PixelFormat::Jpeg => len > 0,
                _ => self.raw_len() == Some(len),
            }
    }
}

/// Metadata describes actual bytes, not merely the requested configuration.
/// Timestamps use the owner-selected monotonic millisecond domain.
/// The source contract defines whether they describe acquisition or observation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capture {
    pub format: Format,
    pub len: usize,
    pub timestamp_ms: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct Frame<'a> {
    pub sequence: u64,
    pub capture: Capture,
    pub bytes: &'a [u8],
}

/// Calls must be bounded. poll_frame must not wait for a future frame.
/// A blocking platform driver belongs behind a worker/timeout adapter, not
/// on a browser event loop. No async executor is required by this contract.
pub trait Camera {
    /// Request JPEG quality before starting capture. Providers without a quality
    /// control return Unsupported; an active provider may return Busy.
    fn set_jpeg_quality(&mut self, _: JpegQuality) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }

    /// Ok reports the negotiated format. On error, attempt cleanup; if cleanup
    /// also fails, retain ownership so the caller can retry stop().
    fn start(&mut self, requested: Format) -> Result<Format, DeviceError>;

    /// None means no frame is ready. The adapter may change destination even
    /// on None/Err; the service must not expose that staging buffer.
    fn poll_frame(
        &mut self,
        now_ms: u64,
        destination: &mut [u8],
    ) -> Result<Option<Capture>, DeviceError>;

    /// Earliest owner-clock millisecond at which another poll may make useful
    /// progress. A returned deadline is advisory for scheduling: the owner may
    /// poll later, and an implementation must still validate the supplied
    /// timestamp in poll_frame().
    ///
    /// None means the device has no time-based retry to advertise. A live
    /// event/interrupt-backed provider can therefore wake its owner through a
    /// separate readiness path instead of forcing periodic polling. Existing
    /// polling-only providers may leave this unimplemented until they expose a
    /// meaningful retry contract.
    fn next_poll_at_ms(&self) -> Option<u64> {
        None
    }

    /// Driver-flagged corrupt frames discarded since the last successful start.
    /// This excludes frames missed by the sensor or skipped by a slow consumer.
    /// None means this provider does not expose that measurement.
    fn discarded_frames(&self) -> Option<u64> {
        None
    }

    /// Success releases the device. On failure it may still be active; the
    /// owner retains it and retries. Implementations must support that retry.
    fn stop(&mut self) -> Result<(), DeviceError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jpeg_quality_validates_normalized_range() {
        for value in 1..=100 {
            assert_eq!(JpegQuality::new(value).unwrap().get(), value);
        }
        for value in [0, 101, u8::MAX] {
            assert_eq!(JpegQuality::new(value), Err(DeviceError::InvalidData));
        }
    }
}

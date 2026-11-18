//! Exclusive microphone capture as fixed-size blocks of mono PCM16.
#![deny(unsafe_code)]

use std::time::Instant;

pub const SAMPLE_RATE_HZ: u32 = 16_000;
pub const SAMPLES_PER_BLOCK: usize = 320;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Io,
    Timeout,
    InvalidData,
    Closed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Frame {
    /// Offset in samples from the beginning of this capture session.
    pub first_sample: u64,
    /// Monotonic observation time of the completion, including queue delay.
    pub observed_at: Instant,
}

pub trait Capture {
    /// Fill one complete block. Only a successful return provides valid samples.
    fn read(
        &mut self,
        until: Instant,
        samples: &mut [i16; SAMPLES_PER_BLOCK],
    ) -> Result<Frame, DeviceError>;

    /// Stop delivery and release the endpoint once buffers have retired.
    /// A timeout retains ownership. Call stop again to finish retirement.
    fn stop(&mut self, until: Instant) -> Result<(), DeviceError>;
}

pub struct Block {
    pub frame: Frame,
    pub samples: [i16; SAMPLES_PER_BLOCK],
}

pub trait Stream {
    /// Take the latest complete block without waiting. Slow readers may skip
    /// blocks. Sample offsets identify gaps, and a terminal error is sticky.
    fn take(&mut self) -> Result<Option<Block>, DeviceError>;

    /// Cancel the native wait and join before surrendering the capture owner.
    fn stop(&mut self) -> Result<(), DeviceError>;
}

mod platform;
pub use platform::{open, stream, AVAILABLE};

#[cfg(any(test, platform = "nuttx"))]
mod session;
#[cfg(any(test, platform = "nuttx"))]
mod worker;

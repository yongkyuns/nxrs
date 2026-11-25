use crate::{Block, Capture, DeviceError, Frame, Stream, SAMPLES_PER_BLOCK};
use std::time::Instant;

pub fn open() -> Result<impl Capture, DeviceError> {
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

pub const AVAILABLE: bool = false;

pub fn stream(_: usize) -> Result<impl Stream, DeviceError> {
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

struct Unavailable;

impl Capture for Unavailable {
    fn read(&mut self, _: Instant, _: &mut [i16; SAMPLES_PER_BLOCK]) -> Result<Frame, DeviceError> {
        Err(DeviceError::Unsupported)
    }

    fn stop(&mut self, _: Instant) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

impl Stream for Unavailable {
    fn take(&mut self) -> Result<Option<Block>, DeviceError> {
        Err(DeviceError::Unsupported)
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

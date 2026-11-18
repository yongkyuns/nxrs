use crate::{Capture, DeviceError, Frame, SAMPLES_PER_BLOCK};
use std::time::Instant;

pub fn open() -> Result<impl Capture, DeviceError> {
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

use crate::{ArmedWatchdog, DeviceError, RuntimeWatchdog};
use std::time::Duration;

struct Unavailable;

impl ArmedWatchdog for Unavailable {
    fn disarm(&mut self) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

impl RuntimeWatchdog for Unavailable {
    fn feed(&mut self) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

pub fn adopt() -> Result<impl ArmedWatchdog, DeviceError> {
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

pub fn arm(_: Duration) -> Result<impl RuntimeWatchdog, DeviceError> {
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

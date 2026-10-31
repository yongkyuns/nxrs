use crate::{ArmedWatchdog, DeviceError};

pub fn adopt() -> Result<impl ArmedWatchdog, DeviceError> {
    struct Unavailable;
    impl ArmedWatchdog for Unavailable {
        fn disarm(&mut self) -> Result<(), DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

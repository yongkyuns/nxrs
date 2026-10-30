use crate::{Control, DeviceError, State};
pub fn open() -> Result<impl Control, DeviceError> {
    struct Unavailable;
    impl Control for Unavailable {
        fn state(&mut self) -> Result<State, DeviceError> {
            Err(DeviceError::Unsupported)
        }
        fn request_trial(&mut self) -> Result<(), DeviceError> {
            Err(DeviceError::Unsupported)
        }
        fn confirm(&mut self) -> Result<(), DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

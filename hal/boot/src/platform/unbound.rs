use crate::{Control, DeviceError, State};
pub fn open() -> Result<impl Control, DeviceError> {
    struct Unavailable;
    impl Control for Unavailable {
        fn stage_update(&mut self, _: &mut dyn std::io::Read, _: u32) -> Result<(), DeviceError> {
            Err(DeviceError::Unsupported)
        }
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

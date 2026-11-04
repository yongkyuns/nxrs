pub use crate::replay::{camera, camera as replay, ReplayCamera};
pub fn open() -> Result<ReplayCamera, crate::DeviceError> {
    Err(crate::DeviceError::Unsupported)
}

//! Capability admission; unsupported real providers are rejected by the profile.
#[cfg(any(test, hal_mock))]
pub(super) mod mock;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::SyntheticImu;
    pub fn open() -> Result<impl crate::Imu + Send + 'static, crate::DeviceError> {
        Ok(SyntheticImu::default())
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{DeviceError, Imu, ImuSample};
    pub struct UnconfiguredImu;

    impl Imu for UnconfiguredImu {
        fn sample(&mut self, _now_ms: u64) -> Result<ImuSample, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }

    pub fn open() -> Result<UnconfiguredImu, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}
pub use selected::*;

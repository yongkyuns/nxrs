//! Capability admission and resource acquisition.
#[cfg(any(test, hal_mock))]
pub(super) mod mock;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
#[path = "nuttx/mod.rs"]
mod selected;
#[cfg(hal_mock)]
mod selected {
    pub fn open() -> Result<impl crate::Imu + Send + 'static, crate::DeviceError> {
        Ok(super::mock::SyntheticImu::default())
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{DeviceError, Imu, ImuSample};
    struct UnconfiguredImu;

    impl Imu for UnconfiguredImu {
        fn sample(&mut self, _now_ms: u64) -> Result<ImuSample, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }

    pub fn open() -> Result<impl Imu + Send + 'static, DeviceError> {
        Err::<UnconfiguredImu, _>(DeviceError::Unsupported)
    }
}
pub use selected::open;

//! IMU capability facade. Provider selection is local to this HAL.
#![no_std]
#![forbid(unsafe_code)]

pub use imu_api::{DeviceError, Imu, ImuSample};

#[cfg(feature = "mock")]
pub fn open() -> Result<impl Imu + Send + 'static, DeviceError> {
    Ok(imu_mock::SyntheticImu::default())
}

#[cfg(not(feature = "mock"))]
pub struct UnconfiguredImu;

#[cfg(not(feature = "mock"))]
impl Imu for UnconfiguredImu {
    fn sample(&mut self, _now_ms: u64) -> Result<ImuSample, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(not(feature = "mock"))]
pub fn open() -> Result<UnconfiguredImu, DeviceError> {
    Err(DeviceError::Unsupported)
}

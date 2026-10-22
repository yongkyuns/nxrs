//! IMU sample contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

mod api;
pub use api::{DeviceError, Imu, ImuSample};

#[cfg(any(test, hal_mock))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(hal_mock)]
pub use mock::SyntheticImu;

#[cfg(hal_mock)]
pub fn open() -> Result<impl Imu + Send + 'static, DeviceError> {
    Ok(SyntheticImu::default())
}

#[cfg(not(any(hal_enabled, hal_mock)))]
pub struct UnconfiguredImu;

#[cfg(not(any(hal_enabled, hal_mock)))]
impl Imu for UnconfiguredImu {
    fn sample(&mut self, _now_ms: u64) -> Result<ImuSample, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(not(any(hal_enabled, hal_mock)))]
pub fn open() -> Result<UnconfiguredImu, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn synthetic_samples_have_increasing_sequences_and_caller_timestamps() {
        let mut imu = mock::SyntheticImu::default();
        let first = imu.sample(10).unwrap();
        let second = imu.sample(20).unwrap();
        assert_eq!((first.sequence, second.sequence), (1, 2));
        assert_eq!((first.timestamp_ms, second.timestamp_ms), (10, 20));
    }
}

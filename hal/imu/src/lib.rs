//! IMU sample contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

mod api;
pub use api::{DeviceError, Imu, ImuSample};

#[cfg(any(test, nxrs_provider = "mock"))]
#[path = "providers/mock.rs"]
mod mock;
#[cfg(nxrs_provider = "mock")]
pub use mock::SyntheticImu;

#[cfg(nxrs_provider = "mock")]
pub fn open() -> Result<impl Imu + Send + 'static, DeviceError> {
    Ok(SyntheticImu::default())
}

#[cfg(nxrs_provider = "none")]
pub struct UnconfiguredImu;

#[cfg(nxrs_provider = "none")]
impl Imu for UnconfiguredImu {
    fn sample(&mut self, _now_ms: u64) -> Result<ImuSample, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(nxrs_provider = "none")]
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

//! IMU contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(test, hal_enabled))]
extern crate std;
mod api;
pub use api::{DeviceError, Imu, ImuSample};
#[cfg(any(test, hal_enabled))]
mod bmi270;
mod platform;
pub use platform::*;

#[cfg(test)]
mod tests {
    use crate::{platform::mock, Imu};

    #[test]
    fn synthetic_samples_have_increasing_sequences_and_caller_timestamps() {
        let mut imu = mock::SyntheticImu::default();
        let first = imu.sample(10).unwrap();
        let second = imu.sample(20).unwrap();
        assert_eq!((first.sequence, second.sequence), (1, 2));
        assert_eq!((first.timestamp_ms, second.timestamp_ms), (10, 20));
    }
}

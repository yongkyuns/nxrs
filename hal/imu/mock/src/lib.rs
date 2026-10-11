//! Deterministic synthetic IMU for architecture demonstrations.
#![no_std]
#![forbid(unsafe_code)]

use imu_api::{DeviceError, Imu, ImuSample};

#[derive(Default)]
pub struct SyntheticImu {
    sequence: u64,
}

impl Imu for SyntheticImu {
    fn sample(&mut self, now_ms: u64) -> Result<ImuSample, DeviceError> {
        self.sequence = self.sequence.saturating_add(1);
        let gyro_z = if self.sequence % 80 < 40 { 0.04 } else { -0.02 };
        Ok(ImuSample {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            accel_mps2: [0.10, 0.0, 9.81],
            gyro_rps: [0.0, 0.0, gyro_z],
        })
    }
}

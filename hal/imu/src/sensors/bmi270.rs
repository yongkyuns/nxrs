//! Translate the reusable BMI270 protocol into the IMU capability contract.
use crate::{DeviceError, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};
use nxrs_bmi270::{Bmi270, Sample};

const SAMPLE_INTERVAL_US: u32 = nxrs_bmi270::SAMPLE_INTERVAL_US;

pub(crate) struct Sensor<B: I2c, D: DelayNs> {
    driver: Bmi270<B, D>,
    sequence: u64,
}

fn map_error(error: nxrs_bmi270::Error) -> DeviceError {
    match error {
        nxrs_bmi270::Error::Bus => DeviceError::Io,
        nxrs_bmi270::Error::InvalidData => DeviceError::InvalidData,
        nxrs_bmi270::Error::Timeout => DeviceError::Timeout,
        nxrs_bmi270::Error::Overflow => DeviceError::Full,
    }
}

fn map_sample(sequence: u64, now_ms: u64, sample: Sample) -> ImuSample {
    ImuSample {
        sequence,
        timestamp_ms: now_ms,
        sample_interval_us: SAMPLE_INTERVAL_US,
        accel_mps2: sample.accel_mps2,
        gyro_rps: sample.gyro_rps,
    }
}

impl<B: I2c, D: DelayNs> Sensor<B, D> {
    pub(crate) fn open(bus: B, address: u8, delay: D) -> Result<Self, DeviceError> {
        Ok(Self {
            driver: Bmi270::open(bus, address, delay).map_err(map_error)?,
            sequence: 0,
        })
    }

    pub(crate) fn stop(&mut self) -> Result<(), DeviceError> {
        self.driver.stop().map_err(map_error)
    }
    pub(crate) fn sample_period(&self) -> core::time::Duration {
        core::time::Duration::from_micros(u64::from(SAMPLE_INTERVAL_US))
    }

    /// `now_ms` is the host reception time shared by the samples in this drain.
    pub(crate) fn drain(
        &mut self,
        now_ms: u64,
        mut emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError> {
        self.driver
            .drain(|sample: Sample| {
                self.sequence = self.sequence.saturating_add(1);
                emit(map_sample(self.sequence, now_ms, sample))
            })
            .map_err(map_error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn driver_errors_keep_the_existing_imu_mapping() {
        assert_eq!(map_error(nxrs_bmi270::Error::Bus), DeviceError::Io);
        assert_eq!(
            map_error(nxrs_bmi270::Error::InvalidData),
            DeviceError::InvalidData
        );
        assert_eq!(map_error(nxrs_bmi270::Error::Timeout), DeviceError::Timeout);
        assert_eq!(map_error(nxrs_bmi270::Error::Overflow), DeviceError::Full);
    }

    #[test]
    fn physical_sample_mapping_keeps_sequence_reception_time_and_odr_interval() {
        let mapped = map_sample(
            7,
            123,
            Sample {
                accel_mps2: [1.0, -2.0, 3.0],
                gyro_rps: [0.25, -0.5, 0.75],
            },
        );
        assert_eq!(mapped.sequence, 7);
        assert_eq!(mapped.timestamp_ms, 123);
        assert_eq!(mapped.sample_interval_us, 10_000);
        assert_eq!(mapped.accel_mps2, [1.0, -2.0, 3.0]);
        assert_eq!(mapped.gyro_rps, [0.25, -0.5, 0.75]);
    }
}

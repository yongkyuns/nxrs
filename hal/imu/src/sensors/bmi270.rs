//! Translate the reusable BMI270 protocol into the IMU capability contract.
use crate::{AccelRange, AppliedConfig, Capabilities, Config, DeviceError, GyroRange, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};
use nxrs_bmi270::{Bmi270, Sample};

const DEFAULT_RATE_MILLIHZ: u32 = 1_000_000_000 / nxrs_bmi270::SAMPLE_INTERVAL_US;

pub(crate) const DEFAULT_CONFIG: Config = Config {
    min_rate_millihz: DEFAULT_RATE_MILLIHZ,
    max_rate_millihz: DEFAULT_RATE_MILLIHZ,
    accel_range: AccelRange::G4,
    gyro_range: GyroRange::Dps2000,
    max_delivery_interval_us: nxrs_bmi270::SAMPLE_INTERVAL_US,
};

const CAPABILITIES: Capabilities = Capabilities {
    rates_millihz: &[25_000, 50_000, DEFAULT_RATE_MILLIHZ, 200_000],
    accel_ranges: &[
        AccelRange::G2,
        AccelRange::G4,
        AccelRange::G8,
        AccelRange::G16,
    ],
    gyro_ranges: &[
        GyroRange::Dps125,
        GyroRange::Dps250,
        GyroRange::Dps500,
        GyroRange::Dps1000,
        GyroRange::Dps2000,
    ],
};

pub(crate) struct Sensor<B: I2c, D: DelayNs> {
    driver: Bmi270<B, D>,
    config: AppliedConfig,
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

fn map_sample(sequence: u64, now_ms: u64, sample_interval_us: u32, sample: Sample) -> ImuSample {
    ImuSample {
        sequence,
        timestamp_ms: now_ms,
        sample_interval_us,
        accel_mps2: sample.accel_mps2,
        gyro_rps: sample.gyro_rps,
    }
}

impl<B: I2c, D: DelayNs> super::Sensor<B, D> for Sensor<B, D> {
    const DEFAULT: Config = DEFAULT_CONFIG;
    const CAPABILITIES: Capabilities = CAPABILITIES;

    fn open(bus: B, address: u8, delay: D, config: AppliedConfig) -> Result<Self, DeviceError> {
        use nxrs_bmi270::{AccelRange as Acc, GyroRange as Gyro, Rate};
        let settings = nxrs_bmi270::Config {
            rate: match config.rate_millihz {
                25_000 => Rate::Hz25,
                50_000 => Rate::Hz50,
                DEFAULT_RATE_MILLIHZ => Rate::Hz100,
                200_000 => Rate::Hz200,
                _ => return Err(DeviceError::Unsupported),
            },
            accel_range: match config.accel_range {
                AccelRange::G2 => Acc::G2,
                AccelRange::G4 => Acc::G4,
                AccelRange::G8 => Acc::G8,
                AccelRange::G16 => Acc::G16,
            },
            gyro_range: match config.gyro_range {
                GyroRange::Dps125 => Gyro::Dps125,
                GyroRange::Dps250 => Gyro::Dps250,
                GyroRange::Dps500 => Gyro::Dps500,
                GyroRange::Dps1000 => Gyro::Dps1000,
                GyroRange::Dps2000 => Gyro::Dps2000,
            },
        };
        Ok(Self {
            driver: Bmi270::open_configured(bus, address, delay, settings).map_err(map_error)?,
            config,
            sequence: 0,
        })
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        self.driver.stop().map_err(map_error)
    }
    fn enable_sensor_reference(&mut self) -> Result<(), DeviceError> {
        self.driver.enable_sensor_reference().map_err(map_error)
    }
    fn applied_config(&self) -> AppliedConfig {
        self.config
    }

    /// `now_ms` is the host reception time shared by the samples in this drain.
    fn drain(
        &mut self,
        now_ms: u64,
        mut emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError> {
        let interval_us = 1_000_000_000u32.div_ceil(self.config.rate_millihz);
        self.driver
            .drain(|sample: Sample| {
                self.sequence = self.sequence.saturating_add(1);
                emit(map_sample(self.sequence, now_ms, interval_us, sample))
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
            nxrs_bmi270::SAMPLE_INTERVAL_US,
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

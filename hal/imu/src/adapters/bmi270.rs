//! Translate the reusable BMI270 protocol into the IMU capability contract.
use super::configuration::select_rate;
use crate::{AppliedConfig, Config, DeviceError, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};
use nxrs_bmi270::{self as driver, Bmi270, Sample};

const DEFAULT_RATE_MILLIHZ: u32 = driver::Rate::Hz100.milli_hz();

pub(crate) const DEFAULT_CONFIG: Config = Config {
    min_rate_millihz: DEFAULT_RATE_MILLIHZ,
    max_rate_millihz: DEFAULT_RATE_MILLIHZ,
    accel_range_g: 4,
    gyro_range_dps: 2000,
    max_delivery_interval_us: nxrs_bmi270::SAMPLE_INTERVAL_US,
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
    type Settings = driver::Config;

    fn resolve(config: Config) -> Result<(Self::Settings, AppliedConfig), DeviceError> {
        let (rate, applied) = select_rate(config, driver::Rate::SUPPORTED, driver::Rate::milli_hz)?;
        let settings = driver::Config {
            rate,
            accel_range: driver::AccelRange::from_g(config.accel_range_g)
                .ok_or(DeviceError::Unsupported)?,
            gyro_range: driver::GyroRange::from_dps(config.gyro_range_dps)
                .ok_or(DeviceError::Unsupported)?,
        };
        Ok((settings, applied))
    }

    fn open(
        bus: B,
        address: u8,
        delay: D,
        settings: Self::Settings,
        config: AppliedConfig,
    ) -> Result<Self, DeviceError> {
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

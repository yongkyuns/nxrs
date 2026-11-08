//! Translate the reusable LSM6DSO protocol into the IMU capability contract.
use super::configuration::select_rate;
use crate::{AppliedConfig, Config, DeviceError, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};
use nxrs_lsm6dso::{self as driver, Lsm6dso, Rate, Sample};

const DEFAULT_RATE: u32 = Rate::Hz104.milli_hz();

pub(crate) struct Sensor<B: I2c> {
    driver: Lsm6dso<B>,
    config: AppliedConfig,
    sequence: u64,
}

fn map_error(error: nxrs_lsm6dso::Error) -> DeviceError {
    match error {
        nxrs_lsm6dso::Error::Bus => DeviceError::Io,
        nxrs_lsm6dso::Error::InvalidData => DeviceError::InvalidData,
        nxrs_lsm6dso::Error::Timeout => DeviceError::Timeout,
        nxrs_lsm6dso::Error::Overflow => DeviceError::Full,
    }
}

impl<B: I2c, D: DelayNs> super::Sensor<B, D> for Sensor<B> {
    const DEFAULT: Config = Config {
        min_rate_millihz: DEFAULT_RATE,
        max_rate_millihz: DEFAULT_RATE,
        accel_range_g: 4,
        gyro_range_dps: 2000,
        max_delivery_interval_us: 1_000_000_000u32.div_ceil(DEFAULT_RATE),
    };
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
            driver: Lsm6dso::open_configured(bus, address, delay, settings).map_err(map_error)?,
            config,
            sequence: 0,
        })
    }

    fn applied_config(&self) -> AppliedConfig {
        self.config
    }

    fn drain(
        &mut self,
        reception_ms: u64,
        mut emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError> {
        let sample_interval_us = 1_000_000_000u32.div_ceil(self.config.rate_millihz);
        self.driver
            .drain(|sample: Sample| {
                self.sequence = self.sequence.saturating_add(1);
                emit(ImuSample {
                    sequence: self.sequence,
                    timestamp_ms: reception_ms,
                    sample_interval_us,
                    accel_mps2: sample.accel_mps2,
                    gyro_rps: sample.gyro_rps,
                })
            })
            .map_err(map_error)
    }

    fn stop(&mut self) -> Result<(), DeviceError> {
        self.driver.stop().map_err(map_error)
    }

    fn enable_sensor_reference(&mut self) -> Result<(), DeviceError> {
        self.driver.enable_sensor_reference().map_err(map_error)
    }
}

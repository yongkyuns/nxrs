//! Translate the reusable LSM6DSO protocol into the IMU capability contract.
use crate::{AccelRange, AppliedConfig, Capabilities, Config, DeviceError, GyroRange, ImuSample};
use embedded_hal::{delay::DelayNs, i2c::I2c};
use nxrs_lsm6dso::{Lsm6dso, Rate, Sample};

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
        accel_range: AccelRange::G4,
        gyro_range: GyroRange::Dps2000,
        max_delivery_interval_us: 1_000_000_000u32.div_ceil(DEFAULT_RATE),
    };
    const CAPABILITIES: Capabilities = Capabilities {
        rates_millihz: &[
            Rate::Hz12_5.milli_hz(),
            Rate::Hz26.milli_hz(),
            Rate::Hz52.milli_hz(),
            DEFAULT_RATE,
            Rate::Hz208.milli_hz(),
        ],
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

    fn open(bus: B, address: u8, delay: D, config: AppliedConfig) -> Result<Self, DeviceError> {
        use nxrs_lsm6dso::{AccelRange as Acc, GyroRange as Gyro};
        let rate = [
            Rate::Hz12_5,
            Rate::Hz26,
            Rate::Hz52,
            Rate::Hz104,
            Rate::Hz208,
        ]
        .into_iter()
        .find(|rate| rate.milli_hz() == config.rate_millihz)
        .ok_or(DeviceError::Unsupported)?;
        let settings = nxrs_lsm6dso::Config {
            rate,
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
}

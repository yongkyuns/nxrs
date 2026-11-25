//! Shared IMU protocol implemented by the concrete driver.
use super::*;
use peripheral::imu;

impl From<Error> for imu::Error {
    fn from(error: Error) -> Self {
        match error {
            Error::Bus => Self::Io,
            Error::InvalidData => Self::InvalidData,
            Error::Timeout => Self::Timeout,
            Error::Overflow => Self::Overflow,
        }
    }
}

impl<T: Registers> imu::Imu for Device<T> {
    const DEFAULT: imu::Config = imu::Config {
        min_rate_millihz: Rate::Hz104.milli_hz(),
        max_rate_millihz: Rate::Hz104.milli_hz(),
        accel_range_g: 4,
        gyro_range_dps: 2000,
        max_delivery_interval_us: 1_000_000_000u32.div_ceil(Rate::Hz104.milli_hz()),
    };
    const BUFFERING: imu::Buffering = imu::Buffering::Fifo;
    type Settings = Config;

    fn resolve(config: imu::Config) -> Result<(Self::Settings, imu::AppliedConfig), imu::Error> {
        let (rate, applied) = imu::select_rate(config, Rate::SUPPORTED, Rate::milli_hz)?;
        let settings = Config {
            rate,
            accel_range: AccelRange::from_g(config.accel_range_g).ok_or(imu::Error::Unsupported)?,
            gyro_range: GyroRange::from_dps(config.gyro_range_dps)
                .ok_or(imu::Error::Unsupported)?,
        };
        Ok((settings, applied))
    }

    fn read_available(
        &mut self,
        mut emit: impl FnMut(imu::Measurement) -> bool,
    ) -> Result<Option<usize>, imu::Error> {
        self.drain(|sample| {
            emit(imu::Measurement {
                accel_mps2: sample.accel_mps2,
                gyro_rps: sample.gyro_rps,
            })
        })
        .map_err(Into::into)
    }

    fn stop(&mut self) -> Result<(), imu::Error> {
        Device::stop(self).map_err(Into::into)
    }
}

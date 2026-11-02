//! Private BMI270 protocol adapter. No OS, device paths or service scheduling.
use crate::{DeviceError, Imu, ImuSample};
use bmi2::{interface::I2cInterface, types::*, Bmi2, I2cAddr};
use embedded_hal::{delay::DelayNs, i2c::I2c};

const BURST: u16 = 32;
type Driver<B, D> = Bmi2<I2cInterface<B>, D, 64>;
pub(crate) struct Sensor<B: I2c, D: DelayNs> {
    driver: Driver<B, D>,
    sequence: u64,
    last_time: Option<u32>,
}
fn error<E>(error: bmi2::types::Error<E>) -> DeviceError {
    match error {
        bmi2::types::Error::Comm(_) => DeviceError::Io,
        _ => DeviceError::InvalidData,
    }
}
impl<B: I2c, D: DelayNs + Default> Sensor<B, D> {
    pub(crate) fn open(mut bus: B, address: u8) -> Result<Self, DeviceError> {
        let address = match address {
            0x68 => I2cAddr::Default,
            0x69 => I2cAddr::Alternative,
            _ => return Err(DeviceError::InvalidData),
        };
        {
            let mut setup =
                Bmi2::<_, _, 64>::new_i2c(&mut bus, D::default(), address, Burst::new(BURST));
            if setup.get_chip_id().map_err(error)? != 0x24 {
                return Err(DeviceError::InvalidData);
            }
            setup
                .init(&bmi2::config::BMI270_CONFIG_FILE)
                .map_err(error)?;
        }
        // bmi2 0.1.2 checks only INTERNAL_STATUS bit0 in init(). Require the
        // exact InitOk byte; avoid its enum decoders, which panic on reserved data.
        let mut status = [0];
        bus.write_read(address.addr(), &[0x21], &mut status)
            .map_err(|_| DeviceError::Io)?;
        if status != [1] {
            return Err(DeviceError::InvalidData);
        }
        let mut driver = Bmi2::new_i2c(bus, D::default(), address, Burst::new(BURST));
        driver.disable_power_save().map_err(error)?;
        driver.set_acc_range(AccRange::Range4g).map_err(error)?;
        driver
            .set_gyr_range(GyrRange {
                range: GyrRangeVal::Range2000,
                ois_range: OisRange::Range2000,
            })
            .map_err(error)?;
        driver
            .set_acc_conf(AccConf {
                odr: Odr::Odr100,
                bwp: AccBwp::NormAvg4,
                filter_perf: PerfMode::Perf,
            })
            .map_err(error)?;
        driver
            .set_gyr_conf(GyrConf {
                odr: Odr::Odr100,
                bwp: GyrBwp::Norm,
                noise_perf: PerfMode::Perf,
                filter_perf: PerfMode::Perf,
            })
            .map_err(error)?;
        driver
            .set_pwr_ctrl(PwrCtrl {
                aux_en: false,
                gyr_en: true,
                acc_en: true,
                temp_en: false,
            })
            .map_err(error)?;
        Ok(Self {
            driver,
            sequence: 0,
            last_time: None,
        })
    }
    pub(crate) fn stop(&mut self) -> Result<(), DeviceError> {
        self.driver
            .set_pwr_ctrl(PwrCtrl {
                aux_en: false,
                gyr_en: false,
                acc_en: false,
                temp_en: false,
            })
            .map_err(error)?;
        let state = self.driver.get_pwr_ctrl().map_err(error)?;
        if state.aux_en || state.gyr_en || state.acc_en || state.temp_en {
            return Err(DeviceError::Io);
        }
        Ok(())
    }
}
impl<B: I2c, D: DelayNs> Imu for Sensor<B, D> {
    fn sample(&mut self, now_ms: u64) -> Result<ImuSample, DeviceError> {
        let status = self.driver.get_status().map_err(error)?;
        if !status.acc_data_ready || !status.gyr_data_ready {
            return Err(DeviceError::Busy);
        }
        // One contiguous read owns both vectors and the sensor counter.
        let data = self.driver.get_data().map_err(error)?;
        if self.last_time == Some(data.time) {
            return Err(DeviceError::Busy);
        }
        self.last_time = Some(data.time);
        self.sequence = self.sequence.saturating_add(1);
        let accel = 9.80665 / 8192.0; // ±4g / signed 16-bit range.
        let gyro = core::f32::consts::PI / 180.0 * 2000.0 / 32768.0;
        Ok(ImuSample {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            accel_mps2: [data.acc.x, data.acc.y, data.acc.z].map(|v| f32::from(v) * accel),
            gyro_rps: [data.gyr.x, data.gyr.y, data.gyr.z].map(|v| f32::from(v) * gyro),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use embedded_hal::i2c::{ErrorKind, ErrorType, Operation};
    #[derive(Default)]
    struct Delay;
    impl DelayNs for Delay {
        fn delay_ns(&mut self, _: u32) {}
    }
    struct Bus {
        status: u8,
        power: u8,
        fail_data: bool,
    }
    impl ErrorType for Bus {
        type Error = ErrorKind;
    }
    impl I2c for Bus {
        fn transaction(&mut self, _: u8, ops: &mut [Operation<'_>]) -> Result<(), Self::Error> {
            match ops {
                [Operation::Write(bytes)] => {
                    if bytes[0] == 0x7d {
                        self.power = bytes[1];
                    }
                }
                [Operation::Write(reg), Operation::Read(bytes)] => {
                    bytes.fill(0);
                    match reg[0] {
                        0x00 => bytes[0] = 0x24,
                        0x21 => bytes[0] = self.status,
                        0x03 => bytes[0] = 0xc0,
                        0x7d => bytes[0] = self.power,
                        0x0c => {
                            if self.fail_data {
                                return Err(ErrorKind::Other);
                            }
                            bytes[0..2].copy_from_slice(&8192_i16.to_le_bytes());
                            bytes[6..8].copy_from_slice(&16384_i16.to_le_bytes());
                            bytes[12] = 1;
                        }
                        _ => {}
                    }
                }
                _ => return Err(ErrorKind::Other),
            }
            Ok(())
        }
    }
    #[test]
    fn rejects_error_initialization_status_including_odd_values() {
        for status in [0, 2, 3, 5, 7, 8, 9, 0xff] {
            assert!(matches!(
                Sensor::<_, Delay>::open(
                    Bus {
                        status,
                        power: 0,
                        fail_data: false
                    },
                    0x68
                ),
                Err(DeviceError::InvalidData)
            ));
        }
    }
    #[test]
    fn converts_one_fresh_burst_and_propagates_failed_reads() {
        let mut sensor = Sensor::<_, Delay>::open(
            Bus {
                status: 1,
                power: 0,
                fail_data: false,
            },
            0x68,
        )
        .unwrap();
        let sample = sensor.sample(100).unwrap();
        assert!((sample.accel_mps2[0] - 9.80665).abs() < 1e-5);
        assert!((sample.gyro_rps[0] - 17.453293).abs() < 1e-5);
        assert_eq!((sample.sequence, sample.timestamp_ms), (1, 100));
        assert_eq!(sensor.sample(101), Err(DeviceError::Busy));
        sensor.stop().unwrap();
        let mut failed = Sensor::<_, Delay>::open(
            Bus {
                status: 1,
                power: 0,
                fail_data: true,
            },
            0x68,
        )
        .unwrap();
        assert_eq!(failed.sample(0), Err(DeviceError::Io));
        assert_eq!(failed.sequence, 0);
    }
}

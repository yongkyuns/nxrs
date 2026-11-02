//! Private BMI270 protocol adapter. No OS, device paths or service scheduling.
use crate::{DeviceError, ImuSample};
use bmi2::{types::*, Bmi2, I2cAddr};
use embedded_hal::{delay::DelayNs, i2c::I2c};

const BURST: u16 = 32;
const ODR: Odr = Odr::Odr100;
// BMI270 ODR codes double the rate per step (code 8 = 100 Hz).
const SAMPLE_INTERVAL_US: u32 = 2_560_000 >> (ODR as u8);
const FRAME_BYTES: u16 = 12;
// Wake for each sample so fusion need not wait for a multi-frame batch.
const WATERMARK_BYTES: u16 = FRAME_BYTES;
pub(crate) const FIFO_WAKE_PERIOD: core::time::Duration =
    core::time::Duration::from_micros(SAMPLE_INTERVAL_US as u64);
pub(crate) struct Sensor<B: I2c, D: DelayNs> {
    bus: B,
    address: I2cAddr,
    _delay: core::marker::PhantomData<D>,
    sequence: u64,
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
        let mut driver =
            Bmi2::<_, D, 64>::new_i2c(&mut bus, D::default(), address, Burst::new(BURST));
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
                odr: ODR,
                bwp: AccBwp::NormAvg4,
                filter_perf: PerfMode::Perf,
            })
            .map_err(error)?;
        driver
            .set_gyr_conf(GyrConf {
                odr: ODR,
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
        // Startup is bounded initialization, not a periodic acquisition path.
        // Different accel/gyro startup times must not enter a headerless FIFO.
        let mut startup_delay = D::default();
        let mut ready = false;
        for _ in 0..100 {
            let status = driver.get_status().map_err(error)?;
            if status.acc_data_ready && status.gyr_data_ready {
                ready = true;
                break;
            }
            startup_delay.delay_ms(1);
        }
        if !ready {
            return Err(DeviceError::Timeout);
        }
        driver
            .set_fifo_downs(FifoDowns {
                gyr_downs: 0,
                gyr_filt_data: FilterData::Filtered,
                acc_downs: 0,
                acc_filt_data: FilterData::Filtered,
            })
            .map_err(error)?;
        driver
            .set_fifo_conf(FifoConf {
                stop_on_full: true,
                time_enable: false,
                tag_int1_en: FifoTagIntEnable::IntEdge,
                tag_int2_en: FifoTagIntEnable::IntEdge,
                header_enable: false,
                aux_enable: false,
                acc_enable: true,
                gyr_enable: true,
            })
            .map_err(error)?;
        driver.set_fifo_wtm(WATERMARK_BYTES).map_err(error)?;
        driver.send_cmd(Cmd::FifoFlush).map_err(error)?;
        driver.set_int_latch(IntLatch::None).map_err(error)?;
        driver
            .set_int1_io_ctrl(IntIoCtrl {
                level: OutputLevel::ActiveHigh,
                od: OutputBehavior::PushPull,
                output_en: true,
                input_en: false,
            })
            .map_err(error)?;
        driver
            .set_int_map_data(IntMapData {
                int1: MapData {
                    ffull: true,
                    fwm: true,
                    drdy: false,
                    err: true,
                },
                int2: MapData {
                    ffull: false,
                    fwm: false,
                    drdy: false,
                    err: false,
                },
            })
            .map_err(error)?;
        // Decoder assumptions must match the configuration actually accepted.
        for (register, expected, mask) in [
            (0x40, ODR as u8, 0x0f),
            (0x42, ODR as u8, 0x0f),
            (0x45, 0x88, 0xff),
            (0x46, WATERMARK_BYTES as u8, 0xff),
            (0x47, (WATERMARK_BYTES >> 8) as u8, 0xff),
            (0x48, 1, 0xff),
            (0x49, 0xc0, 0xff),
            (0x53, 0x0a, 0xff),
            (0x55, 0, 1),
            (0x58, 0x0b, 0xff),
        ] {
            let mut value = [0];
            bus.write_read(address.addr(), &[register], &mut value)
                .map_err(|_| DeviceError::Io)?;
            if value[0] & mask != expected {
                return Err(DeviceError::InvalidData);
            }
        }
        Ok(Self {
            bus,
            address,
            _delay: core::marker::PhantomData,
            sequence: 0,
        })
    }
    pub(crate) fn stop(&mut self) -> Result<(), DeviceError> {
        let mut driver =
            Bmi2::<_, D, 64>::new_i2c(&mut self.bus, D::default(), self.address, Burst::new(BURST));
        driver
            .set_int_map_data(IntMapData {
                int1: MapData {
                    ffull: false,
                    fwm: false,
                    drdy: false,
                    err: false,
                },
                int2: MapData {
                    ffull: false,
                    fwm: false,
                    drdy: false,
                    err: false,
                },
            })
            .map_err(error)?;
        driver
            .set_int1_io_ctrl(IntIoCtrl {
                level: OutputLevel::ActiveHigh,
                od: OutputBehavior::PushPull,
                output_en: false,
                input_en: false,
            })
            .map_err(error)?;
        driver
            .set_pwr_ctrl(PwrCtrl {
                aux_en: false,
                gyr_en: false,
                acc_en: false,
                temp_en: false,
            })
            .map_err(error)?;
        let state = driver.get_pwr_ctrl().map_err(error)?;
        if state.aux_en || state.gyr_en || state.acc_en || state.temp_en {
            return Err(DeviceError::Io);
        }
        Ok(())
    }
}
impl<B: I2c, D: DelayNs> Sensor<B, D> {
    /// Drain after an IRQ or ODR-derived wake. FIFO contents alone admit samples.
    pub(crate) fn drain(
        &mut self,
        now_ms: u64,
        mut emit: impl FnMut(ImuSample) -> bool,
    ) -> Result<Option<usize>, DeviceError> {
        let mut status = [0];
        self.bus
            .write_read(self.address.addr(), &[0x1d], &mut status)
            .map_err(|_| DeviceError::Io)?;
        if status[0] & 1 != 0 {
            return Err(DeviceError::Full);
        }
        if status[0] & 4 != 0 {
            return Err(DeviceError::Io);
        }
        let mut length = [0; 2];
        self.bus
            .write_read(self.address.addr(), &[0x24], &mut length)
            .map_err(|_| DeviceError::Io)?;
        let count = usize::from(u16::from_le_bytes(length));
        // Headerless frames contain gyro XYZ followed by accel XYZ (12 bytes).
        // Equal ODR, no auxiliary data/tags/time, and no downsampling are required.
        if count > 2048 || count % 12 != 0 {
            return Err(DeviceError::InvalidData);
        }
        let mut remaining = count;
        let mut bytes = [0; 60];
        while remaining != 0 {
            let n = remaining.min(bytes.len());
            self.bus
                .write_read(self.address.addr(), &[0x26], &mut bytes[..n])
                .map_err(|_| DeviceError::Io)?;
            for frame in bytes[..n].chunks_exact(12) {
                self.sequence = self.sequence.saturating_add(1);
                let axis = |i| f32::from(i16::from_le_bytes([frame[i], frame[i + 1]]));
                let accel = 9.80665 / 8192.0;
                let gyro = core::f32::consts::PI / 180.0 * 2000.0 / 32768.0;
                if !emit(ImuSample {
                    sequence: self.sequence,
                    timestamp_ms: now_ms,
                    sample_interval_us: SAMPLE_INTERVAL_US,
                    accel_mps2: [axis(6) * accel, axis(8) * accel, axis(10) * accel],
                    gyro_rps: [axis(0) * gyro, axis(2) * gyro, axis(4) * gyro],
                }) {
                    return Ok(None);
                }
            }
            remaining -= n;
        }
        Ok(Some(count / 12))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use embedded_hal::i2c::{ErrorKind, ErrorType, Operation};
    use std::vec::Vec;
    #[derive(Default)]
    struct Delay;
    impl DelayNs for Delay {
        fn delay_ns(&mut self, _: u32) {}
    }
    struct Bus {
        registers: [u8; 128],
        fifo: Vec<u8>,
        cursor: usize,
        fail_data: bool,
        startup_reads: usize,
        ready_after: usize,
    }
    impl Bus {
        fn new() -> Self {
            let mut registers = [0; 128];
            registers[0] = 0x24;
            registers[0x21] = 1;
            Self {
                registers,
                fifo: Vec::new(),
                cursor: 0,
                fail_data: false,
                startup_reads: 0,
                ready_after: 3,
            }
        }
    }
    impl ErrorType for Bus {
        type Error = ErrorKind;
    }
    impl I2c for Bus {
        fn transaction(
            &mut self,
            _: u8,
            operations: &mut [Operation<'_>],
        ) -> Result<(), Self::Error> {
            match operations {
                [Operation::Write(bytes)] => {
                    let reg = usize::from(bytes[0]);
                    if reg == 0x49 && bytes[1] & 0xc0 != 0 {
                        assert!(
                            self.startup_reads > self.ready_after,
                            "FIFO enabled before both sensors ready"
                        );
                    }
                    if reg != 0x5e {
                        self.registers[reg..reg + bytes.len() - 1].copy_from_slice(&bytes[1..]);
                    }
                }
                [Operation::Write(reg), Operation::Read(bytes)] => match reg[0] {
                    0x03 => {
                        self.startup_reads += 1;
                        bytes[0] = if self.startup_reads > self.ready_after {
                            0xc0
                        } else {
                            0x80
                        };
                    }
                    0x24 => bytes.copy_from_slice(
                        &(self.fifo.len() as u16 - self.cursor as u16).to_le_bytes(),
                    ),
                    0x26 => {
                        if self.fail_data {
                            return Err(ErrorKind::Other);
                        }
                        assert!(bytes.len() <= 60 && bytes.len() % 12 == 0);
                        bytes.copy_from_slice(&self.fifo[self.cursor..self.cursor + bytes.len()]);
                        self.cursor += bytes.len();
                    }
                    _ => bytes.copy_from_slice(
                        &self.registers[usize::from(reg[0])..usize::from(reg[0]) + bytes.len()],
                    ),
                },
                _ => return Err(ErrorKind::Other),
            }
            Ok(())
        }
    }
    #[test]
    fn initialization_requires_exact_status_and_configures_only_fifo_interrupts() {
        for status in [0, 2, 3, 5, 7, 8, 9, 0xff] {
            let mut bus = Bus::new();
            bus.registers[0x21] = status;
            assert!(matches!(
                Sensor::<_, Delay>::open(bus, 0x68),
                Err(DeviceError::InvalidData)
            ));
        }
        let mut sensor = Sensor::<_, Delay>::open(Bus::new(), 0x68).unwrap();
        assert_eq!(sensor.bus.registers[0x40] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(sensor.bus.registers[0x42] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(&sensor.bus.registers[0x46..0x48], &[12, 0]);
        assert_eq!(sensor.bus.registers[0x49], 0xc0);
        assert_eq!(sensor.bus.registers[0x58], 0x0b); // WM/full/error; no DRDY polling.
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0);
        assert_eq!(sensor.bus.registers[0x7d], 0);
    }
    #[test]
    fn fifo_requires_both_sensors_ready_and_initialization_has_a_deadline() {
        let mut bus = Bus::new();
        bus.ready_after = 100;
        assert!(matches!(
            Sensor::<_, Delay>::open(bus, 0x68),
            Err(DeviceError::Timeout)
        ));
        let sensor = Sensor::<_, Delay>::open(Bus::new(), 0x68).unwrap();
        assert_eq!(sensor.bus.startup_reads, 4);
    }
    #[test]
    fn each_available_frame_is_delivered_without_waiting_for_a_batch() {
        let mut sensor = Sensor::<_, Delay>::open(Bus::new(), 0x68).unwrap();
        for sequence in 1..=3 {
            sensor.bus.fifo.extend_from_slice(&[0; 12]);
            let mut samples = Vec::new();
            assert_eq!(
                sensor.drain(sequence * 10, |sample| {
                    samples.push(sample);
                    true
                }),
                Ok(Some(1))
            );
            assert_eq!(samples.len(), 1);
            assert_eq!(
                (samples[0].sequence, samples[0].sample_interval_us),
                (sequence, 10_000)
            );
            assert_eq!(
                sensor.drain(sequence * 10, |_| panic!("duplicate sample")),
                Ok(Some(0))
            );
        }
    }
    #[test]
    fn fifo_bursts_preserve_all_samples_order_units_and_nominal_interval() {
        let mut sensor = Sensor::<_, Delay>::open(Bus::new(), 0x68).unwrap();
        for _ in 0..7 {
            for axis in [0_i16, 0, 16384, 8192, -8192, 0] {
                sensor.bus.fifo.extend_from_slice(&axis.to_le_bytes());
            }
        }
        let mut samples = Vec::new();
        assert_eq!(
            sensor.drain(123, |sample| {
                samples.push(sample);
                true
            }),
            Ok(Some(7))
        );
        assert_eq!(samples.len(), 7);
        for (i, sample) in samples.iter().enumerate() {
            assert_eq!(
                (
                    sample.sequence,
                    sample.timestamp_ms,
                    sample.sample_interval_us
                ),
                (i as u64 + 1, 123, 10_000)
            );
            assert!((sample.accel_mps2[0] - 9.80665).abs() < 1e-5);
            assert!((sample.accel_mps2[1] + 9.80665).abs() < 1e-5);
            assert!((sample.gyro_rps[2] - 17.453293).abs() < 1e-5);
        }
        assert_eq!(sensor.drain(124, |_| panic!("replayed FIFO")), Ok(Some(0)));
    }
    #[test]
    fn overflow_partial_frames_io_and_closed_consumers_are_explicit() {
        let mut sensor = Sensor::<_, Delay>::open(Bus::new(), 0x68).unwrap();
        sensor.bus.registers[0x1d] = 1;
        assert_eq!(sensor.drain(0, |_| true), Err(DeviceError::Full));
        sensor.bus.registers[0x1d] = 0;
        sensor.bus.fifo.push(0);
        assert_eq!(sensor.drain(0, |_| true), Err(DeviceError::InvalidData));
        sensor.bus.fifo.resize(24, 0);
        sensor.bus.fail_data = true;
        assert_eq!(sensor.drain(0, |_| true), Err(DeviceError::Io));
        sensor.bus.fail_data = false;
        assert_eq!(sensor.drain(0, |_| false), Ok(None));
        assert_eq!(sensor.sequence, 1);
    }
}

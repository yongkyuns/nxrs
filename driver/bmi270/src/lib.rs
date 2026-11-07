//! Reusable BMI270 protocol driver. No OS, device paths or service scheduling.
#![no_std]
#![forbid(unsafe_code)]
#[cfg(test)]
extern crate std;

use bmi2::{types::*, Bmi2, I2cAddr};

use embedded_hal::{delay::DelayNs, i2c::I2c};

const BURST: u16 = 32;
const FRAME_BYTES: u16 = 12;
// One complete frame is enough to raise a FIFO watermark interrupt.
const WATERMARK_BYTES: u16 = FRAME_BYTES;
pub struct Bmi270<B: I2c, D: DelayNs> {
    bus: B,
    delay: D,
    address: I2cAddr,
    config: Config,
    reference_enabled: bool,
}

/// Sensor-specific acquisition settings. Defaults preserve the established HAL behavior.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub rate: Rate,
    pub accel_range: AccelRange,
    pub gyro_range: GyroRange,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            rate: Rate::Hz100,
            accel_range: AccelRange::G4,
            gyro_range: GyroRange::Dps2000,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rate {
    Hz25,
    Hz50,
    Hz100,
    Hz200,
}

impl Rate {
    const fn odr(self) -> Odr {
        match self {
            Self::Hz25 => Odr::Odr25,
            Self::Hz50 => Odr::Odr50,
            Self::Hz100 => Odr::Odr100,
            Self::Hz200 => Odr::Odr200,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AccelRange {
    G2,
    G4,
    G8,
    G16,
}

impl AccelRange {
    const fn register(self) -> AccRange {
        match self {
            Self::G2 => AccRange::Range2g,
            Self::G4 => AccRange::Range4g,
            Self::G8 => AccRange::Range8g,
            Self::G16 => AccRange::Range16g,
        }
    }

    const fn full_scale_g(self) -> f32 {
        match self {
            Self::G2 => 2.0,
            Self::G4 => 4.0,
            Self::G8 => 8.0,
            Self::G16 => 16.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GyroRange {
    Dps125,
    Dps250,
    Dps500,
    Dps1000,
    Dps2000,
}

impl GyroRange {
    const fn register(self) -> GyrRangeVal {
        match self {
            Self::Dps125 => GyrRangeVal::Range125,
            Self::Dps250 => GyrRangeVal::Range250,
            Self::Dps500 => GyrRangeVal::Range500,
            Self::Dps1000 => GyrRangeVal::Range1000,
            Self::Dps2000 => GyrRangeVal::Range2000,
        }
    }

    const fn full_scale_dps(self) -> f32 {
        match self {
            Self::Dps125 => 125.0,
            Self::Dps250 => 250.0,
            Self::Dps500 => 500.0,
            Self::Dps1000 => 1000.0,
            Self::Dps2000 => 2000.0,
        }
    }
}

// Retained for consumers that use the driver's established default cadence.
// BMI270 ODR codes double the rate per step (code 8 = 100 Hz).
pub const SAMPLE_INTERVAL_US: u32 = 2_560_000 >> (Odr::Odr100 as u8);
/// Failures from BMI270 protocol operations, independent of any capability HAL.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Bus,
    InvalidData,
    Timeout,
    Overflow,
}

/// One decoded physical observation. Timing and delivery sequence belong to the consumer.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    pub accel_mps2: [f32; 3],
    pub gyro_rps: [f32; 3],
}

fn error<E>(error: bmi2::types::Error<E>) -> Error {
    match error {
        bmi2::types::Error::Comm(_) => Error::Bus,
        _ => Error::InvalidData,
    }
}
impl<B: I2c, D: DelayNs> Bmi270<B, D> {
    pub fn open(bus: B, address: u8, delay: D) -> Result<Self, Error> {
        Self::open_configured(bus, address, delay, Config::default())
    }

    pub fn open_configured(
        mut bus: B,
        address: u8,
        mut delay: D,
        config: Config,
    ) -> Result<Self, Error> {
        let address = match address {
            0x68 => I2cAddr::Default,
            0x69 => I2cAddr::Alternative,
            _ => return Err(Error::InvalidData),
        };
        {
            let mut setup =
                Bmi2::<_, _, 64>::new_i2c(&mut bus, &mut delay, address, Burst::new(BURST));
            if setup.get_chip_id().map_err(error)? != 0x24 {
                return Err(Error::InvalidData);
            }
            setup
                .init(&bmi2::config::BMI270_CONFIG_FILE)
                .map_err(error)?;
        }
        // bmi2 0.1.2 checks only INTERNAL_STATUS bit0 in init(). Require the
        // exact InitOk byte; avoid its enum decoders, which panic on reserved data.
        let mut status = [0];
        bus.write_read(address.addr(), &[0x21], &mut status)
            .map_err(|_| Error::Bus)?;
        if status != [1] {
            return Err(Error::InvalidData);
        }
        let mut driver =
            Bmi2::<_, _, 64>::new_i2c(&mut bus, &mut delay, address, Burst::new(BURST));
        driver.disable_power_save().map_err(error)?;
        driver
            .set_acc_range(config.accel_range.register())
            .map_err(error)?;
        driver
            .set_gyr_range(GyrRange {
                range: config.gyro_range.register(),
                ois_range: OisRange::Range2000,
            })
            .map_err(error)?;
        driver
            .set_acc_conf(AccConf {
                odr: config.rate.odr(),
                bwp: AccBwp::NormAvg4,
                filter_perf: PerfMode::Perf,
            })
            .map_err(error)?;
        driver
            .set_gyr_conf(GyrConf {
                odr: config.rate.odr(),
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
        let mut ready = false;
        for _ in 0..100 {
            let mut startup =
                Bmi2::<_, _, 64>::new_i2c(&mut bus, &mut delay, address, Burst::new(BURST));
            let status = startup.get_status().map_err(error)?;
            if status.acc_data_ready && status.gyr_data_ready {
                ready = true;
                break;
            }
            delay.delay_ms(1);
        }
        if !ready {
            return Err(Error::Timeout);
        }
        let mut driver =
            Bmi2::<_, _, 64>::new_i2c(&mut bus, &mut delay, address, Burst::new(BURST));
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
            (0x40, config.rate.odr() as u8, 0x0f),
            (0x42, config.rate.odr() as u8, 0x0f),
            (0x41, config.accel_range.register() as u8, 0x03),
            // The pinned bmi2 decoder masks away the valid 125 dps code (4).
            // Read the physical range and unchanged OIS setting from the register.
            (0x43, config.gyro_range.register() as u8 | 0x08, 0x0f),
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
                .map_err(|_| Error::Bus)?;
            if value[0] & mask != expected {
                return Err(Error::InvalidData);
            }
        }
        Ok(Self {
            bus,
            delay,
            address,
            config,
            reference_enabled: false,
        })
    }

    /// Route the shared accelerometer and gyroscope data-ready signal to INT2.
    /// Call `stop` if setup fails after accessing the sensor.
    pub fn enable_sensor_reference(&mut self) -> Result<(), Error> {
        // Once the first write is attempted, the ordinary stop path owns cleanup.
        self.reference_enabled = true;
        let mut driver = Bmi2::<_, _, 64>::new_i2c(
            &mut self.bus,
            &mut self.delay,
            self.address,
            Burst::new(BURST),
        );
        let map = driver.get_int_map_data().map_err(error)?.to_reg() | IntMapDataMask::DRDY_INT2;
        driver
            .set_int_map_data(IntMapData::from_reg(map))
            .map_err(error)?;
        let pin = IntIoCtrl {
            level: OutputLevel::ActiveHigh,
            od: OutputBehavior::PushPull,
            output_en: true,
            input_en: false,
        };
        let pin_config = pin.to_reg();
        driver
            .set_int2_io_ctrl(IntIoCtrl::from_reg(pin_config))
            .map_err(error)?;
        if driver.get_int_map_data().map_err(error)?.to_reg() != map
            || driver.get_int2_io_ctrl().map_err(error)?.to_reg() != pin_config
        {
            return Err(Error::InvalidData);
        }
        Ok(())
    }
    pub fn stop(&mut self) -> Result<(), Error> {
        let reference_enabled = self.reference_enabled;
        let mut driver = Bmi2::<_, _, 64>::new_i2c(
            &mut self.bus,
            &mut self.delay,
            self.address,
            Burst::new(BURST),
        );
        let reference_stopped = if reference_enabled {
            let stopped = driver
                .set_int2_io_ctrl(IntIoCtrl {
                    level: OutputLevel::ActiveHigh,
                    od: OutputBehavior::PushPull,
                    output_en: false,
                    input_en: false,
                })
                .map_err(error)
                .and_then(|()| {
                    let pin = driver.get_int2_io_ctrl().map_err(error)?;
                    if pin.output_en || pin.input_en {
                        return Err(Error::InvalidData);
                    }
                    Ok(())
                });
            if stopped.is_ok() {
                self.reference_enabled = false;
            }
            stopped
        } else {
            Ok(())
        };
        driver
            .set_int_map_data(IntMapData::from_reg(0))
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
            return Err(Error::Bus);
        }
        if reference_enabled && driver.get_int_map_data().map_err(error)?.to_reg() != 0 {
            return Err(Error::InvalidData);
        }
        reference_stopped
    }

    /// Drain after an IRQ or ODR-derived wake. FIFO contents alone admit samples.
    pub fn drain(&mut self, mut emit: impl FnMut(Sample) -> bool) -> Result<Option<usize>, Error> {
        let mut status = [0];
        self.bus
            .write_read(self.address.addr(), &[0x1d], &mut status)
            .map_err(|_| Error::Bus)?;
        if status[0] & 1 != 0 {
            return Err(Error::Overflow);
        }
        if status[0] & 4 != 0 {
            return Err(Error::Bus);
        }
        let mut length = [0; 2];
        self.bus
            .write_read(self.address.addr(), &[0x24], &mut length)
            .map_err(|_| Error::Bus)?;
        let count = usize::from(u16::from_le_bytes(length));
        // Headerless frames contain gyro XYZ followed by accel XYZ (12 bytes).
        // Equal ODR, no auxiliary data/tags/time, and no downsampling are required.
        if count > 2048 || count % 12 != 0 {
            return Err(Error::InvalidData);
        }
        let mut remaining = count;
        let mut bytes = [0; 60];
        let accel = 9.80665 * self.config.accel_range.full_scale_g() / 32768.0;
        let gyro =
            core::f32::consts::PI / 180.0 * self.config.gyro_range.full_scale_dps() / 32768.0;
        while remaining != 0 {
            let n = remaining.min(bytes.len());
            self.bus
                .write_read(self.address.addr(), &[0x26], &mut bytes[..n])
                .map_err(|_| Error::Bus)?;
            for frame in bytes[..n].chunks_exact(12) {
                let axis = |i| f32::from(i16::from_le_bytes([frame[i], frame[i + 1]]));
                if !emit(Sample {
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
    struct Delay {
        startup_waits: u8,
    }
    impl Delay {
        fn new() -> Self {
            Self { startup_waits: 0 }
        }
    }
    impl DelayNs for Delay {
        fn delay_ns(&mut self, ns: u32) {
            self.startup_waits += u8::from(ns == 1_000_000);
        }
    }
    struct Bus {
        registers: [u8; 128],
        fifo: Vec<u8>,
        cursor: usize,
        fail_data: bool,
        mismatch_read: Option<(u8, u8)>,
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
                mismatch_read: None,
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
                    _ => {
                        bytes.copy_from_slice(
                            &self.registers[usize::from(reg[0])..usize::from(reg[0]) + bytes.len()],
                        );
                        if let Some((mismatch_reg, mask)) = self.mismatch_read {
                            if reg[0] == mismatch_reg && !bytes.is_empty() {
                                bytes[0] ^= mask;
                            }
                        }
                    }
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
                Bmi270::<_, Delay>::open(bus, 0x68, Delay::new()),
                Err(Error::InvalidData)
            ));
        }
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        assert_eq!(sensor.delay.startup_waits, 3);
        assert_eq!(sensor.bus.registers[0x40] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(sensor.bus.registers[0x42] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(sensor.bus.registers[0x41] & 0x03, AccRange::Range4g as u8);
        assert_eq!(sensor.bus.registers[0x43] & 0x0f, 0x08);
        assert_eq!(&sensor.bus.registers[0x46..0x48], &[12, 0]);
        assert_eq!(sensor.bus.registers[0x49], 0xc0);
        assert_eq!(sensor.bus.registers[0x58], 0x0b); // WM/full/error; no DRDY polling.
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0);
        assert_eq!(sensor.bus.registers[0x7d], 0);
    }

    #[test]
    fn optional_shared_drdy_reference_uses_int2_and_cleans_up() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0x0b);
        assert_eq!(sensor.bus.registers[0x54], 0);
        sensor.bus.registers[0x58] |= 0x80;
        sensor.enable_sensor_reference().unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0xcb);
        assert_eq!(sensor.bus.registers[0x54], 0x0a);
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0);
        assert_eq!(sensor.bus.registers[0x54], 0x02);
    }

    #[test]
    fn optional_shared_drdy_reference_readback_failure_is_cleaned_by_stop() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        sensor.bus.mismatch_read = Some((0x58, 0x40));
        assert_eq!(sensor.enable_sensor_reference(), Err(Error::InvalidData));
        sensor.bus.mismatch_read = None;
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.registers[0x58], 0);
        assert_eq!(sensor.bus.registers[0x54], 0x02);
    }

    #[test]
    fn configured_rate_and_ranges_are_read_back_and_used_for_conversion() {
        let config = Config {
            rate: Rate::Hz50,
            accel_range: AccelRange::G8,
            gyro_range: GyroRange::Dps125,
        };
        let mut sensor =
            Bmi270::<_, Delay>::open_configured(Bus::new(), 0x68, Delay::new(), config).unwrap();
        assert_eq!(sensor.bus.registers[0x40] & 0x0f, Odr::Odr50 as u8);
        assert_eq!(sensor.bus.registers[0x42] & 0x0f, Odr::Odr50 as u8);
        assert_eq!(sensor.bus.registers[0x41] & 0x03, AccRange::Range8g as u8);
        assert_eq!(sensor.bus.registers[0x43] & 0x0f, 0x0c);
        for axis in [0_i16, 0, 16384, 16384, 0, 0] {
            sensor.bus.fifo.extend_from_slice(&axis.to_le_bytes());
        }
        let mut sample = None;
        assert_eq!(
            sensor.drain(|value| {
                sample = Some(value);
                true
            }),
            Ok(Some(1))
        );
        let sample = sample.unwrap();
        assert!((sample.accel_mps2[0] - 4.0 * 9.80665).abs() < 1e-4);
        assert!((sample.gyro_rps[2] - 125.0 * core::f32::consts::PI / 180.0 / 2.0).abs() < 1e-5);
    }

    #[test]
    fn configured_initialization_rejects_readback_mismatch() {
        let mut bus = Bus::new();
        bus.mismatch_read = Some((0x43, 0x01));
        assert_eq!(
            Bmi270::<_, Delay>::open_configured(bus, 0x68, Delay::new(), Config::default(),).err(),
            Some(Error::InvalidData)
        );
    }
    #[test]
    fn fifo_requires_both_sensors_ready_and_initialization_has_a_deadline() {
        let mut bus = Bus::new();
        bus.ready_after = 100;
        assert!(matches!(
            Bmi270::<_, Delay>::open(bus, 0x68, Delay::new()),
            Err(Error::Timeout)
        ));
        let sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        assert_eq!(sensor.bus.startup_reads, 4);
    }
    #[test]
    fn each_available_frame_is_delivered_without_waiting_for_a_batch() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        for _ in 1..=3 {
            sensor.bus.fifo.extend_from_slice(&[0; 12]);
            let mut samples = Vec::new();
            assert_eq!(
                sensor.drain(|sample| {
                    samples.push(sample);
                    true
                }),
                Ok(Some(1))
            );
            assert_eq!(samples.len(), 1);
            assert_eq!(
                (samples[0].accel_mps2, samples[0].gyro_rps),
                ([0.0; 3], [0.0; 3])
            );
            assert_eq!(sensor.drain(|_| panic!("duplicate sample")), Ok(Some(0)));
        }
    }
    #[test]
    fn fifo_bursts_preserve_all_samples_order_units_and_nominal_interval() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        for _ in 0..7 {
            for axis in [0_i16, 0, 16384, 8192, -8192, 0] {
                sensor.bus.fifo.extend_from_slice(&axis.to_le_bytes());
            }
        }
        let mut samples = Vec::new();
        assert_eq!(
            sensor.drain(|sample| {
                samples.push(sample);
                true
            }),
            Ok(Some(7))
        );
        assert_eq!(samples.len(), 7);
        for sample in &samples {
            assert!((sample.accel_mps2[0] - 9.80665).abs() < 1e-5);
            assert!((sample.accel_mps2[1] + 9.80665).abs() < 1e-5);
            assert!((sample.gyro_rps[2] - 17.453293).abs() < 1e-5);
        }
        assert_eq!(sensor.drain(|_| panic!("replayed FIFO")), Ok(Some(0)));
    }
    #[test]
    fn overflow_partial_frames_io_and_closed_consumers_are_explicit() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        sensor.bus.registers[0x1d] = 1;
        assert_eq!(sensor.drain(|_| true), Err(Error::Overflow));
        sensor.bus.registers[0x1d] = 0;
        sensor.bus.fifo.push(0);
        assert_eq!(sensor.drain(|_| true), Err(Error::InvalidData));
        sensor.bus.fifo.resize(24, 0);
        sensor.bus.fail_data = true;
        assert_eq!(sensor.drain(|_| true), Err(Error::Bus));
        sensor.bus.fail_data = false;
        assert_eq!(sensor.drain(|_| false), Ok(None));
    }
}

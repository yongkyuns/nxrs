//! Reusable BMI270 protocol driver. No OS, device paths or service scheduling.
#![forbid(unsafe_code)]

mod imu;

use bmi2::types::*;
mod transport;
use transport::Registers;
pub use transport::{I2cBus, SpiBus};

use embedded_hal::{delay::DelayNs, i2c::I2c, spi::SpiDevice};

const CONFIG_CHUNK: usize = 30;
const FRAME_BYTES: u16 = 12;
// One complete frame is enough to raise a FIFO watermark interrupt.
const WATERMARK_BYTES: u16 = FRAME_BYTES;
pub type Bmi270<B, D> = Device<I2cBus<B>, D>;
pub type Bmi270Spi<S, D> = Device<SpiBus<S>, D>;

pub struct Device<T, D> {
    io: T,
    delay: D,
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
    /// Paired accelerometer/gyroscope rates supported by this driver's FIFO setup.
    pub const SUPPORTED: [Self; 4] = [Self::Hz25, Self::Hz50, Self::Hz100, Self::Hz200];

    pub const fn milli_hz(self) -> u32 {
        match self {
            Self::Hz25 => 25_000,
            Self::Hz50 => 50_000,
            Self::Hz100 => 100_000,
            Self::Hz200 => 200_000,
        }
    }

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
    /// Resolve an exact full scale in multiples of standard gravity.
    pub const fn from_g(value: u16) -> Option<Self> {
        match value {
            2 => Some(Self::G2),
            4 => Some(Self::G4),
            8 => Some(Self::G8),
            16 => Some(Self::G16),
            _ => None,
        }
    }

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
    /// Resolve an exact full scale in degrees per second.
    pub const fn from_dps(value: u16) -> Option<Self> {
        match value {
            125 => Some(Self::Dps125),
            250 => Some(Self::Dps250),
            500 => Some(Self::Dps500),
            1000 => Some(Self::Dps1000),
            2000 => Some(Self::Dps2000),
            _ => None,
        }
    }

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

impl<B: I2c, D: DelayNs> Bmi270<B, D> {
    pub fn open(bus: B, address: u8, delay: D) -> Result<Self, Error> {
        Self::open_configured(bus, address, delay, Config::default())
    }
    pub fn open_configured(bus: B, address: u8, delay: D, config: Config) -> Result<Self, Error> {
        Self::from_interface(I2cBus::new(bus, address)?, delay, config)
    }
}
impl<S: SpiDevice, D: DelayNs> Bmi270Spi<S, D> {
    pub fn open_spi(device: S, delay: D, config: Config) -> Result<Self, Error> {
        Self::from_interface(SpiBus::new(device), delay, config)
    }
}
impl<T: Registers, D: DelayNs> Device<T, D> {
    pub fn from_interface(io: T, delay: D, config: Config) -> Result<Self, Error> {
        let mut sensor = Self {
            io,
            delay,
            config,
            reference_enabled: false,
        };
        sensor.initialize()?;
        Ok(sensor)
    }
    fn read(&mut self, register: u8, bytes: &mut [u8]) -> Result<(), Error> {
        self.io.read(register, bytes)
    }
    fn read_reg(&mut self, register: u8) -> Result<u8, Error> {
        self.io.read_reg(register)
    }
    fn write(&mut self, register: u8, bytes: &[u8]) -> Result<(), Error> {
        self.io.write(register, bytes)?;
        // SPI requires a write-to-next-access gap even with power save disabled.
        if T::WRITE_DELAY_NS != 0 {
            self.delay.delay_ns(T::WRITE_DELAY_NS);
        }
        Ok(())
    }
    fn write_reg(&mut self, register: u8, value: u8) -> Result<(), Error> {
        self.write(register, &[value])
    }
    fn initialize(&mut self) -> Result<(), Error> {
        self.io.initialize()?;
        if self.read_reg(0)? != 0x24 {
            return Err(Error::InvalidData);
        }
        self.write_reg(0x7e, 0xb6)?;
        self.delay.delay_us(2_000);
        self.io.initialize()?; // Soft reset also resets SPI selection.
        let power = self.read_reg(0x7c)? & !1;
        self.write_reg(0x7c, power)?;
        self.delay.delay_us(450);
        self.write_reg(0x59, 0)?;
        // Keep the pinned Bosch image and bounded, even-sized chunks. Wire
        // framing is ours; bmi2 0.1.2's SPI read/write framing is not used.
        for (index, chunk) in bmi2::config::BMI270_CONFIG_FILE
            .chunks(CONFIG_CHUNK)
            .enumerate()
        {
            let word = (index * CONFIG_CHUNK / 2) as u16;
            self.write(0x5b, &[(word & 0x0f) as u8, (word >> 4) as u8])?;
            self.write(0x5e, chunk)?;
            self.delay.delay_us(2);
        }
        self.write_reg(0x59, 1)?;
        self.delay.delay_us(20_000);
        if self.read_reg(0x21)? != 1 {
            return Err(Error::InvalidData);
        }
        let config = self.config;
        for (register, value) in [
            (0x41, config.accel_range.register() as u8),
            (0x43, config.gyro_range.register() as u8 | 0x08),
            (
                0x40,
                AccConf {
                    odr: config.rate.odr(),
                    bwp: AccBwp::NormAvg4,
                    filter_perf: PerfMode::Perf,
                }
                .to_reg(),
            ),
            (
                0x42,
                GyrConf {
                    odr: config.rate.odr(),
                    bwp: GyrBwp::Norm,
                    noise_perf: PerfMode::Perf,
                    filter_perf: PerfMode::Perf,
                }
                .to_reg(),
            ),
            (0x7d, 0x06),
        ] {
            self.write_reg(register, value)?;
        }
        let mut ready = false;
        for _ in 0..100 {
            if self.read_reg(0x03)? & 0xc0 == 0xc0 {
                ready = true;
                break;
            }
            self.delay.delay_ms(1);
        }
        if !ready {
            return Err(Error::Timeout);
        }
        for (register, value) in [
            (0x45, 0x88),
            (0x48, 1),
            (0x49, 0xc0),
            (0x46, WATERMARK_BYTES as u8),
            (0x47, (WATERMARK_BYTES >> 8) as u8),
            (0x7e, 0xb0),
            (0x55, 0),
            (0x53, 0x0a),
            (0x58, 0x0b),
        ] {
            self.write_reg(register, value)?;
        }
        for (register, expected, mask) in [
            (0x40, config.rate.odr() as u8, 0x0f),
            (0x42, config.rate.odr() as u8, 0x0f),
            (0x41, config.accel_range.register() as u8, 0x03),
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
            if self.read_reg(register)? & mask != expected {
                return Err(Error::InvalidData);
            }
        }
        Ok(())
    }

    /// Route the shared accelerometer and gyroscope data-ready signal to INT2.
    /// Call `stop` if setup fails after accessing the sensor.
    pub fn enable_sensor_reference(&mut self) -> Result<(), Error> {
        self.reference_enabled = true;
        let map = self.read_reg(0x58)? | 0xc0;
        self.write_reg(0x58, map)?;
        self.write_reg(0x54, 0x0a)?;
        if self.read_reg(0x58)? != map || self.read_reg(0x54)? != 0x0a {
            return Err(Error::InvalidData);
        }
        Ok(())
    }
    pub fn stop(&mut self) -> Result<(), Error> {
        let reference = self.reference_enabled;
        let reference_stopped = if reference {
            self.write_reg(0x54, 0x02).and_then(|()| {
                if self.read_reg(0x54)? & 0x18 != 0 {
                    Err(Error::InvalidData)
                } else {
                    Ok(())
                }
            })
        } else {
            Ok(())
        };
        if reference_stopped.is_ok() {
            self.reference_enabled = false;
        }
        self.write_reg(0x58, 0)?;
        self.write_reg(0x53, 0x02)?;
        self.write_reg(0x7d, 0)?;
        if self.read_reg(0x7d)? & 0x0f != 0 {
            return Err(Error::Bus);
        }
        if reference && self.read_reg(0x58)? != 0 {
            return Err(Error::InvalidData);
        }
        reference_stopped
    }

    /// Drain after an IRQ or ODR-derived wake. FIFO contents alone admit samples.
    pub fn drain(&mut self, mut emit: impl FnMut(Sample) -> bool) -> Result<Option<usize>, Error> {
        let mut status = [0];
        self.read(0x1d, &mut status)?;
        if status[0] & 1 != 0 {
            return Err(Error::Overflow);
        }
        if status[0] & 4 != 0 {
            return Err(Error::Bus);
        }
        let mut length = [0; 2];
        self.read(0x24, &mut length)?;
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
            self.read(0x26, &mut bytes[..n])?;
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
    use peripheral::imu::Imu as _;
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
        assert_eq!(sensor.io.bus.registers[0x40] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(sensor.io.bus.registers[0x42] & 0x0f, Odr::Odr100 as u8);
        assert_eq!(
            sensor.io.bus.registers[0x41] & 0x03,
            AccRange::Range4g as u8
        );
        assert_eq!(sensor.io.bus.registers[0x43] & 0x0f, 0x08);
        assert_eq!(&sensor.io.bus.registers[0x46..0x48], &[12, 0]);
        assert_eq!(sensor.io.bus.registers[0x49], 0xc0);
        assert_eq!(sensor.io.bus.registers[0x58], 0x0b); // WM/full/error; no DRDY polling.
        sensor.stop().unwrap();
        assert_eq!(sensor.io.bus.registers[0x58], 0);
        assert_eq!(sensor.io.bus.registers[0x7d], 0);
    }

    #[test]
    fn optional_shared_drdy_reference_uses_int2_and_cleans_up() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        assert_eq!(sensor.io.bus.registers[0x58], 0x0b);
        assert_eq!(sensor.io.bus.registers[0x54], 0);
        sensor.io.bus.registers[0x58] |= 0x80;
        sensor.enable_sensor_reference().unwrap();
        assert_eq!(sensor.io.bus.registers[0x58], 0xcb);
        assert_eq!(sensor.io.bus.registers[0x54], 0x0a);
        sensor.stop().unwrap();
        assert_eq!(sensor.io.bus.registers[0x58], 0);
        assert_eq!(sensor.io.bus.registers[0x54], 0x02);
    }

    #[test]
    fn optional_shared_drdy_reference_readback_failure_is_cleaned_by_stop() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        sensor.io.bus.mismatch_read = Some((0x58, 0x40));
        assert_eq!(sensor.enable_sensor_reference(), Err(Error::InvalidData));
        sensor.io.bus.mismatch_read = None;
        sensor.stop().unwrap();
        assert_eq!(sensor.io.bus.registers[0x58], 0);
        assert_eq!(sensor.io.bus.registers[0x54], 0x02);
    }

    #[test]
    fn configured_rate_and_ranges_are_read_back_and_used_for_conversion() {
        let (config, applied) = Bmi270::<Bus, Delay>::resolve(peripheral::imu::Config {
            min_rate_millihz: 30_000,
            max_rate_millihz: 60_000,
            accel_range_g: 8,
            gyro_range_dps: 125,
            max_delivery_interval_us: 20_000,
        })
        .unwrap();
        assert_eq!(applied.rate_millihz, 50_000);
        assert_eq!(
            Bmi270::<Bus, Delay>::BUFFERING,
            peripheral::imu::Buffering::Fifo
        );
        let mut sensor =
            Bmi270::<_, Delay>::open_configured(Bus::new(), 0x68, Delay::new(), config).unwrap();
        assert_eq!(sensor.io.bus.registers[0x40] & 0x0f, Odr::Odr50 as u8);
        assert_eq!(sensor.io.bus.registers[0x42] & 0x0f, Odr::Odr50 as u8);
        assert_eq!(
            sensor.io.bus.registers[0x41] & 0x03,
            AccRange::Range8g as u8
        );
        assert_eq!(sensor.io.bus.registers[0x43] & 0x0f, 0x0c);
        for axis in [0_i16, 0, 16384, 16384, 0, 0] {
            sensor.io.bus.fifo.extend_from_slice(&axis.to_le_bytes());
        }
        let mut sample = None;
        assert_eq!(
            sensor.read_available(|value| {
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
        assert_eq!(sensor.io.bus.startup_reads, 4);
    }
    #[test]
    fn each_available_frame_is_delivered_without_waiting_for_a_batch() {
        let mut sensor = Bmi270::<_, Delay>::open(Bus::new(), 0x68, Delay::new()).unwrap();
        for _ in 1..=3 {
            sensor.io.bus.fifo.extend_from_slice(&[0; 12]);
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
                sensor.io.bus.fifo.extend_from_slice(&axis.to_le_bytes());
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
        sensor.io.bus.registers[0x1d] = 1;
        assert_eq!(
            sensor.read_available(|_| true),
            Err(peripheral::imu::Error::Overflow)
        );
        sensor.io.bus.registers[0x1d] = 0;
        sensor.io.bus.fifo.push(0);
        assert_eq!(
            sensor.read_available(|_| true),
            Err(peripheral::imu::Error::InvalidData)
        );
        sensor.io.bus.fifo.resize(24, 0);
        sensor.io.bus.fail_data = true;
        assert_eq!(
            sensor.read_available(|_| true),
            Err(peripheral::imu::Error::Io)
        );
        sensor.io.bus.fail_data = false;
        assert_eq!(sensor.read_available(|_| false), Ok(None));
    }
    include!("spi_tests.rs");
}

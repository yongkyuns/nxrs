//! LSM6DSO register and tagged FIFO driver. OS and HAL policy live elsewhere.
#![forbid(unsafe_code)]

mod imu;

use embedded_hal::{delay::DelayNs, i2c::I2c};

const WHO_AM_I: u8 = 0x0f;
const CTRL1_XL: u8 = 0x10;
const CTRL2_G: u8 = 0x11;
const CTRL3_C: u8 = 0x12;
const FIFO_CTRL1: u8 = 0x07;
const FIFO_CTRL2: u8 = 0x08;
const FIFO_CTRL3: u8 = 0x09;
const FIFO_CTRL4: u8 = 0x0a;
const INT1_CTRL: u8 = 0x0d;
const INT2_CTRL: u8 = 0x0e;
const COUNTER_BDR_REG1: u8 = 0x0b;
const FIFO_STATUS1: u8 = 0x3a;
const FIFO_DATA_OUT_TAG: u8 = 0x78;
const FIFO_WATERMARK_WORDS: u16 = 4;
// The part has 3 KiB of ordinary FIFO storage; each uncompressed word is 7 bytes.
const FIFO_CAPACITY_WORDS: usize = 3072 / 7;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Bus,
    InvalidData,
    Timeout,
    Overflow,
}

/// Exact paired sensor output rates supported by this driver's FIFO setup.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rate {
    Hz12_5,
    Hz26,
    Hz52,
    Hz104,
    Hz208,
}

impl Rate {
    /// Paired accelerometer/gyroscope rates supported by this driver's FIFO setup.
    pub const SUPPORTED: [Self; 5] = [
        Self::Hz12_5,
        Self::Hz26,
        Self::Hz52,
        Self::Hz104,
        Self::Hz208,
    ];

    pub const fn milli_hz(self) -> u32 {
        match self {
            Self::Hz12_5 => 12_500,
            Self::Hz26 => 26_000,
            Self::Hz52 => 52_000,
            Self::Hz104 => 104_000,
            Self::Hz208 => 208_000,
        }
    }

    /// Rounded integer microseconds; use `milli_hz` when exact cadence matters.
    pub const fn interval_us(self) -> u32 {
        match self {
            Self::Hz12_5 => 80_000,
            Self::Hz26 => 38_462,
            Self::Hz52 => 19_231,
            Self::Hz104 => 9_615,
            Self::Hz208 => 4_808,
        }
    }

    const fn register(self) -> u8 {
        match self {
            Self::Hz12_5 => 0x1,
            Self::Hz26 => 0x2,
            Self::Hz52 => 0x3,
            Self::Hz104 => 0x4,
            Self::Hz208 => 0x5,
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

    const fn bits(self) -> u8 {
        match self {
            Self::G2 => 0x00,
            Self::G16 => 0x04,
            Self::G4 => 0x08,
            Self::G8 => 0x0c,
        }
    }

    /// ST DS12140 Rev 3 nominal sensitivity in mg per LSB.
    const fn sensitivity_mg_per_lsb(self) -> f32 {
        match self {
            Self::G2 => 0.061,
            Self::G4 => 0.122,
            Self::G8 => 0.244,
            Self::G16 => 0.488,
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

    const fn bits(self) -> u8 {
        match self {
            Self::Dps125 => 0x02,
            Self::Dps250 => 0x00,
            Self::Dps500 => 0x04,
            Self::Dps1000 => 0x08,
            Self::Dps2000 => 0x0c,
        }
    }

    /// ST DS12140 Rev 3 nominal sensitivity in mdps per LSB.
    const fn sensitivity_mdps_per_lsb(self) -> f32 {
        match self {
            Self::Dps125 => 4.375,
            Self::Dps250 => 8.75,
            Self::Dps500 => 17.5,
            Self::Dps1000 => 35.0,
            Self::Dps2000 => 70.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub rate: Rate,
    pub accel_range: AccelRange,
    pub gyro_range: GyroRange,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            rate: Rate::Hz104,
            accel_range: AccelRange::G4,
            gyro_range: GyroRange::Dps2000,
        }
    }
}

/// A normalized simultaneous accel/gyro observation. Timing and sequence are consumer-owned.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    pub accel_mps2: [f32; 3],
    pub gyro_rps: [f32; 3],
    /// The two-bit FIFO time-slot counter shared by the source records.
    pub slot: u8,
}

/// Decoded individual tagged FIFO word. Unconfigured and control tags are retained explicitly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FifoRecord {
    Gyroscope { slot: u8, xyz: [i16; 3] },
    Accelerometer { slot: u8, xyz: [i16; 3] },
    Other { tag: u8, slot: u8, xyz: [i16; 3] },
}

/// Decode one seven-byte FIFO word and reject a bad even-parity tag.
pub fn parse_fifo_record(bytes: [u8; 7]) -> Result<FifoRecord, Error> {
    let tag = bytes[0];
    if tag.count_ones() & 1 != 0 {
        return Err(Error::InvalidData);
    }
    let sensor = tag >> 3;
    let slot = (tag >> 1) & 0x03;
    let xyz = [
        i16::from_le_bytes([bytes[1], bytes[2]]),
        i16::from_le_bytes([bytes[3], bytes[4]]),
        i16::from_le_bytes([bytes[5], bytes[6]]),
    ];
    match sensor {
        1 => Ok(FifoRecord::Gyroscope { slot, xyz }),
        2 => Ok(FifoRecord::Accelerometer { slot, xyz }),
        _ => Ok(FifoRecord::Other {
            tag: sensor,
            slot,
            xyz,
        }),
    }
}

pub struct Lsm6dso<B> {
    bus: B,
    address: u8,
    config: Config,
    pending_accel: Option<(u8, [i16; 3])>,
    pending_gyro: Option<(u8, [i16; 3])>,
    reference_state: Option<(u8, u8)>,
}

fn read_reg<B: I2c>(bus: &mut B, address: u8, reg: u8) -> Result<u8, Error> {
    let mut value = [0];
    bus.write_read(address, &[reg], &mut value)
        .map_err(|_| Error::Bus)?;
    Ok(value[0])
}

fn write_reg<B: I2c>(bus: &mut B, address: u8, reg: u8, value: u8) -> Result<(), Error> {
    bus.write(address, &[reg, value]).map_err(|_| Error::Bus)
}

impl<B: I2c> Lsm6dso<B> {
    pub fn open<D: DelayNs>(bus: B, address: u8, delay: D) -> Result<Self, Error> {
        Self::open_configured(bus, address, delay, Config::default())
    }

    pub fn open_configured<D: DelayNs>(
        mut bus: B,
        address: u8,
        mut delay: D,
        config: Config,
    ) -> Result<Self, Error> {
        if !matches!(address, 0x6a | 0x6b) {
            return Err(Error::InvalidData);
        }
        if read_reg(&mut bus, address, WHO_AM_I)? != 0x6c {
            return Err(Error::InvalidData);
        }
        let setup = (|| {
            // SW_RESET self-clears; poll it with a hard upper bound.
            write_reg(&mut bus, address, CTRL3_C, 0x01)?;
            let mut reset_done = false;
            for _ in 0..20 {
                if read_reg(&mut bus, address, CTRL3_C)? & 1 == 0 {
                    reset_done = true;
                    break;
                }
                delay.delay_ms(1);
            }
            if !reset_done {
                return Err(Error::Timeout);
            }

            // BDU + register auto-increment; first leave FIFO in bypass.
            write_reg(&mut bus, address, CTRL3_C, 0x44)?;
            write_reg(&mut bus, address, FIFO_CTRL4, 0x00)?;
            let xl = config.rate.register() << 4 | config.accel_range.bits();
            let gyro = config.rate.register() << 4 | config.gyro_range.bits();
            write_reg(&mut bus, address, CTRL1_XL, xl)?;
            write_reg(&mut bus, address, CTRL2_G, gyro)?;
            // Batching ODRs equal the live ODRs; only accel and gyro are batched.
            write_reg(&mut bus, address, FIFO_CTRL1, FIFO_WATERMARK_WORDS as u8)?;
            write_reg(
                &mut bus,
                address,
                FIFO_CTRL2,
                ((FIFO_WATERMARK_WORDS >> 8) as u8) & 1,
            )?;
            write_reg(
                &mut bus,
                address,
                FIFO_CTRL3,
                config.rate.register() | (config.rate.register() << 4),
            )?;
            // Continuous mode; compression, timestamp, and temperature batching stay disabled.
            write_reg(&mut bus, address, FIFO_CTRL4, 0x06)?;
            // INT1 reports threshold, full-soon and overrun status to the HAL worker.
            write_reg(&mut bus, address, INT1_CTRL, 0x38)?;

            for (reg, expected, mask) in [
                (CTRL3_C, 0x44, 0x44),
                (CTRL1_XL, xl, 0xfc),
                (CTRL2_G, gyro, 0xfe),
                (FIFO_CTRL1, FIFO_WATERMARK_WORDS as u8, 0xff),
                (FIFO_CTRL2, 0, 0x7f),
                (
                    FIFO_CTRL3,
                    config.rate.register() | (config.rate.register() << 4),
                    0xff,
                ),
                (FIFO_CTRL4, 0x06, 0x07),
                (INT1_CTRL, 0x38, 0x38),
            ] {
                if read_reg(&mut bus, address, reg)? & mask != expected & mask {
                    return Err(Error::InvalidData);
                }
            }
            Ok(())
        })();
        if let Err(error) = setup {
            // Cleanup is bounded and best-effort; preserve the initiating failure.
            let _ = write_reg(&mut bus, address, FIFO_CTRL4, 0x00);
            let _ = write_reg(&mut bus, address, INT1_CTRL, 0x00);
            let _ = write_reg(&mut bus, address, CTRL1_XL, 0x00);
            let _ = write_reg(&mut bus, address, CTRL2_G, 0x00);
            return Err(error);
        }
        Ok(Self {
            bus,
            address,
            config,
            pending_accel: None,
            pending_gyro: None,
            reference_state: None,
        })
    }

    /// Route the accelerometer output-ready signal to INT2 as a 75 us pulse.
    /// Call `stop` if setup fails after accessing the sensor.
    pub fn enable_sensor_reference(&mut self) -> Result<(), Error> {
        if self.reference_state.is_some() {
            return Err(Error::InvalidData);
        }
        let old_int2 = read_reg(&mut self.bus, self.address, INT2_CTRL)?;
        let old_counter = read_reg(&mut self.bus, self.address, COUNTER_BDR_REG1)?;
        self.reference_state = Some((old_int2, old_counter));
        write_reg(
            &mut self.bus,
            self.address,
            COUNTER_BDR_REG1,
            old_counter | 0x80,
        )?;
        write_reg(&mut self.bus, self.address, INT2_CTRL, old_int2 | 0x01)?;
        if read_reg(&mut self.bus, self.address, COUNTER_BDR_REG1)? != (old_counter | 0x80)
            || read_reg(&mut self.bus, self.address, INT2_CTRL)? != (old_int2 | 0x01)
        {
            return Err(Error::InvalidData);
        }
        Ok(())
    }

    pub const fn config(&self) -> Config {
        self.config
    }

    pub fn stop(&mut self) -> Result<(), Error> {
        let mut first_error = None;
        let reference_state = self.reference_state;
        if let Some((old_int2, old_counter)) = reference_state {
            for (reg, value) in [(INT2_CTRL, old_int2), (COUNTER_BDR_REG1, old_counter)] {
                if write_reg(&mut self.bus, self.address, reg, value).is_err()
                    && first_error.is_none()
                {
                    first_error = Some(Error::Bus);
                }
            }
        }
        for reg in [INT1_CTRL, FIFO_CTRL4, CTRL1_XL, CTRL2_G] {
            if write_reg(&mut self.bus, self.address, reg, 0).is_err() && first_error.is_none() {
                first_error = Some(Error::Bus);
            }
        }
        for (reg, mask) in [
            (FIFO_CTRL4, 0x07),
            (INT1_CTRL, 0x38),
            (CTRL1_XL, 0xf0),
            (CTRL2_G, 0xf0),
        ] {
            match read_reg(&mut self.bus, self.address, reg) {
                Ok(value) if value & mask == 0 => {}
                Ok(_) if first_error.is_none() => first_error = Some(Error::InvalidData),
                Err(error) if first_error.is_none() => first_error = Some(error),
                _ => {}
            }
        }
        if let Some((old_int2, old_counter)) = reference_state {
            let int_ok = read_reg(&mut self.bus, self.address, INT2_CTRL) == Ok(old_int2);
            let counter_ok =
                read_reg(&mut self.bus, self.address, COUNTER_BDR_REG1) == Ok(old_counter);
            if !int_ok || !counter_ok {
                if first_error.is_none() {
                    first_error = Some(Error::InvalidData);
                }
            } else {
                self.reference_state = None;
            }
        }
        first_error.map_or(Ok(()), Err)
    }

    /// Drain all currently reported tagged words, pairing accel/gyro records by FIFO slot.
    /// `None` means the consumer declined a sample; no later words are read in that call.
    pub fn drain(&mut self, mut emit: impl FnMut(Sample) -> bool) -> Result<Option<usize>, Error> {
        let mut status = [0u8; 2];
        self.bus
            .write_read(self.address, &[FIFO_STATUS1], &mut status)
            .map_err(|_| Error::Bus)?;
        // STATUS2: bit7 watermark, bit6 overrun, bit5 full-at-next-ODR,
        // bit4 batch-counter, bit3 latched overrun, bit2 reserved, bits1:0 count.
        if status[1] & 0x68 != 0 {
            return Err(Error::Overflow);
        }
        if status[1] & 0x14 != 0 {
            return Err(Error::InvalidData);
        }
        let count = usize::from(u16::from_le_bytes([status[0], status[1] & 0x03]));
        if count > FIFO_CAPACITY_WORDS {
            return Err(Error::InvalidData);
        }
        let accel_scale = 9.80665 * self.config.accel_range.sensitivity_mg_per_lsb() / 1000.0;
        let gyro_scale = core::f32::consts::PI / 180.0
            * self.config.gyro_range.sensitivity_mdps_per_lsb()
            / 1000.0;
        let mut read = 0usize;
        let mut completed = 0usize;
        let mut word = [0u8; 7];
        while read < count {
            self.bus
                .write_read(self.address, &[FIFO_DATA_OUT_TAG], &mut word)
                .map_err(|_| Error::Bus)?;
            match parse_fifo_record(word)? {
                FifoRecord::Gyroscope { slot, xyz } => {
                    if self.pending_gyro.is_some()
                        || self
                            .pending_accel
                            .is_some_and(|(pending_slot, _)| pending_slot != slot)
                    {
                        return Err(Error::InvalidData);
                    }
                    self.pending_gyro = Some((slot, xyz));
                }
                FifoRecord::Accelerometer { slot, xyz } => {
                    if self.pending_accel.is_some()
                        || self
                            .pending_gyro
                            .is_some_and(|(pending_slot, _)| pending_slot != slot)
                    {
                        return Err(Error::InvalidData);
                    }
                    self.pending_accel = Some((slot, xyz));
                }
                FifoRecord::Other { .. } => return Err(Error::InvalidData),
            }
            if let (Some((gs, g)), Some((aslot, a))) = (self.pending_gyro, self.pending_accel) {
                if gs != aslot {
                    return Err(Error::InvalidData);
                }
                let sample = Sample {
                    accel_mps2: [
                        a[0] as f32 * accel_scale,
                        a[1] as f32 * accel_scale,
                        a[2] as f32 * accel_scale,
                    ],
                    gyro_rps: [
                        g[0] as f32 * gyro_scale,
                        g[1] as f32 * gyro_scale,
                        g[2] as f32 * gyro_scale,
                    ],
                    slot: gs,
                };
                self.pending_accel = None;
                self.pending_gyro = None;
                if !emit(sample) {
                    return Ok(None);
                }
                completed += 1;
            }
            read += 1;
        }
        Ok(Some(completed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use embedded_hal::i2c::{ErrorKind, ErrorType, Operation};
    use peripheral::imu::Imu as _;
    use std::vec::Vec;

    struct Delay;
    impl DelayNs for Delay {
        fn delay_ns(&mut self, _: u32) {}
    }

    struct Bus {
        regs: [u8; 128],
        fifo: Vec<u8>,
        cursor: usize,
        reset_stuck: bool,
        mismatch: Option<(u8, u8)>,
        status_flags: u8,
        failed_write: Option<u8>,
        write_attempts: std::vec::Vec<u8>,
        status_bursts: usize,
    }
    impl Bus {
        fn new() -> Self {
            let mut regs = [0; 128];
            regs[WHO_AM_I as usize] = 0x6c;
            Self {
                regs,
                fifo: Vec::new(),
                cursor: 0,
                reset_stuck: false,
                mismatch: None,
                status_flags: 0,
                failed_write: None,
                write_attempts: std::vec::Vec::new(),
                status_bursts: 0,
            }
        }
        fn push(&mut self, sensor: u8, slot: u8, xyz: [i16; 3]) {
            let mut tag = (sensor << 3) | ((slot & 0x03) << 1);
            if tag.count_ones() & 1 != 0 {
                tag |= 1;
            }
            self.fifo.push(tag);
            for value in xyz {
                self.fifo.extend_from_slice(&value.to_le_bytes());
            }
        }
    }
    impl ErrorType for Bus {
        type Error = ErrorKind;
    }
    impl I2c for Bus {
        fn transaction(&mut self, _: u8, ops: &mut [Operation<'_>]) -> Result<(), Self::Error> {
            match ops {
                [Operation::Write(bytes)] => {
                    self.write_attempts.push(bytes[0]);
                    if self.failed_write == Some(bytes[0]) {
                        return Err(ErrorKind::Other);
                    }
                    let reg = bytes[0] as usize;
                    if reg == CTRL3_C as usize && bytes[1] & 1 != 0 && !self.reset_stuck {
                        self.regs[reg] = 0;
                    } else {
                        self.regs[reg..reg + bytes.len() - 1].copy_from_slice(&bytes[1..]);
                    }
                }
                [Operation::Write(reg), Operation::Read(bytes)] => match reg[0] {
                    FIFO_STATUS1 if bytes.len() == 2 => {
                        self.status_bursts += 1;
                        let words = ((self.fifo.len() - self.cursor) / 7) as u16;
                        bytes[0] = words as u8;
                        bytes[1] = ((words >> 8) as u8) | self.status_flags;
                    }
                    FIFO_DATA_OUT_TAG => {
                        assert_eq!(bytes.len(), 7);
                        bytes.copy_from_slice(&self.fifo[self.cursor..self.cursor + 7]);
                        self.cursor += 7;
                    }
                    _ => {
                        let index = reg[0] as usize;
                        for (i, byte) in bytes.iter_mut().enumerate() {
                            *byte = self.regs[index + i];
                        }
                        if let Some((mismatch_reg, mask)) = self.mismatch {
                            if mismatch_reg == reg[0] && !bytes.is_empty() {
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
    fn configuration_uses_exact_chip_rate_and_ranges_with_readback() {
        let sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        assert_eq!(sensor.config().rate.milli_hz(), 104_000);
        assert_eq!(sensor.bus.regs[CTRL1_XL as usize], 0x48);
        assert_eq!(sensor.bus.regs[CTRL2_G as usize], 0x4c);
        assert_eq!(
            sensor.bus.regs[FIFO_CTRL1 as usize],
            FIFO_WATERMARK_WORDS as u8
        );
        assert_eq!(sensor.bus.regs[FIFO_CTRL4 as usize], 0x06);
        assert_eq!(sensor.bus.regs[INT1_CTRL as usize], 0x38);
        let config = Config {
            rate: Rate::Hz26,
            accel_range: AccelRange::G16,
            gyro_range: GyroRange::Dps125,
        };
        let sensor = Lsm6dso::open_configured(Bus::new(), 0x6b, Delay, config).unwrap();
        assert_eq!(sensor.bus.regs[CTRL1_XL as usize], 0x24);
        assert_eq!(sensor.bus.regs[CTRL2_G as usize], 0x22);
    }

    #[test]
    fn optional_accel_ready_reference_is_pulsed_on_int2_and_cleans_up() {
        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        sensor.bus.regs[INT2_CTRL as usize] = 0x40;
        sensor.bus.regs[COUNTER_BDR_REG1 as usize] = 0x05;
        sensor.enable_sensor_reference().unwrap();
        assert_eq!(sensor.bus.regs[INT1_CTRL as usize], 0x38);
        assert_eq!(sensor.bus.regs[INT2_CTRL as usize], 0x41);
        assert_eq!(sensor.bus.regs[COUNTER_BDR_REG1 as usize], 0x85);
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.regs[INT2_CTRL as usize], 0x40);
        assert_eq!(sensor.bus.regs[COUNTER_BDR_REG1 as usize], 0x05);
        assert_eq!(sensor.bus.regs[INT1_CTRL as usize], 0);
    }

    #[test]
    fn optional_accel_ready_reference_partial_write_failure_cleans_up() {
        let bus = Bus::new();
        let mut sensor = Lsm6dso::open(bus, 0x6a, Delay).unwrap();
        sensor.bus.failed_write = Some(COUNTER_BDR_REG1);
        assert_eq!(sensor.enable_sensor_reference(), Err(Error::Bus));
        sensor.bus.failed_write = None;
        sensor.stop().unwrap();
        assert_eq!(sensor.bus.regs[INT2_CTRL as usize], 0);
        assert_eq!(sensor.bus.regs[COUNTER_BDR_REG1 as usize], 0);
        assert_eq!(sensor.bus.regs[INT1_CTRL as usize], 0);
    }

    #[test]
    fn identity_reset_timeout_and_readback_mismatch_are_bounded() {
        let mut bus = Bus::new();
        bus.regs[WHO_AM_I as usize] = 0;
        assert_eq!(
            Lsm6dso::open(bus, 0x6a, Delay).err(),
            Some(Error::InvalidData)
        );
        let mut bus = Bus::new();
        bus.reset_stuck = true;
        assert_eq!(Lsm6dso::open(bus, 0x6a, Delay).err(), Some(Error::Timeout));
        let mut bus = Bus::new();
        bus.mismatch = Some((CTRL1_XL, 0x10));
        assert_eq!(
            Lsm6dso::open(bus, 0x6a, Delay).err(),
            Some(Error::InvalidData)
        );
        assert_eq!(
            Lsm6dso::open(Bus::new(), 0x68, Delay).err(),
            Some(Error::InvalidData)
        );
    }

    #[test]
    fn tagged_parser_and_drain_reject_invalid_data_and_overrun() {
        let mut word = [0u8; 7];
        word[0] = 0x08 | 0x06;
        if word[0].count_ones() & 1 != 0 {
            word[0] |= 1;
        }
        word[1..3].copy_from_slice(&(-32768i16).to_le_bytes());
        assert_eq!(
            parse_fifo_record(word),
            Ok(FifoRecord::Gyroscope {
                slot: 3,
                xyz: [-32768, 0, 0]
            })
        );
        word[0] ^= 1;
        assert_eq!(parse_fifo_record(word), Err(Error::InvalidData));

        let mut bus = Bus::new();
        bus.push(1, 0, [16384, 0, -16384]);
        bus.push(2, 0, [8192, -8192, 0]);
        let mut sensor = Lsm6dso::open(bus, 0x6a, Delay).unwrap();
        let mut emitted = None;
        assert_eq!(
            sensor.drain(|sample| {
                emitted = Some(sample);
                true
            }),
            Ok(Some(1))
        );
        let sample = emitted.unwrap();
        assert_eq!(sensor.bus.status_bursts, 1);
        assert_eq!(sample.slot, 0);
        assert!((sample.accel_mps2[0] - 9.80665 * 0.999424).abs() < 0.001);
        assert!((sample.gyro_rps[0] - 16384.0 * 0.070_f32.to_radians()).abs() < 0.001);

        sensor.bus.status_flags = 0x40;
        assert_eq!(sensor.drain(|_| true), Err(Error::Overflow));
    }

    #[test]
    fn pending_tagged_record_survives_fifo_snapshot_boundary() {
        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        sensor.bus.push(1, 2, [100, 0, 0]);
        assert_eq!(sensor.drain(|_| true), Ok(Some(0)));
        assert_eq!(sensor.pending_gyro, Some((2, [100, 0, 0])));
        sensor.bus.push(2, 2, [200, 0, 0]);
        let mut sample = None;
        assert_eq!(
            sensor.drain(|s| {
                sample = Some(s);
                true
            }),
            Ok(Some(1))
        );
        assert_eq!(sample.unwrap().slot, 2);
        assert_eq!(sensor.pending_gyro, None);
        assert_eq!(sensor.pending_accel, None);
        sensor.bus.push(1, 3, [0; 3]);
        sensor.bus.push(2, 3, [0; 3]);
        let mut calls = 0;
        assert_eq!(
            sensor.read_available(|_| {
                calls += 1;
                false
            }),
            Ok(None)
        );
        assert_eq!(calls, 1);
        sensor.bus.status_flags = 0x40;
        assert_eq!(
            sensor.read_available(|_| panic!("overflow sample")),
            Err(peripheral::imu::Error::Overflow)
        );
    }

    #[test]
    fn raw_conversion_matches_ds12140_nominal_sensitivities_for_every_range() {
        let accel_cases = [
            (AccelRange::G2, 0.061),
            (AccelRange::G4, 0.122),
            (AccelRange::G8, 0.244),
            (AccelRange::G16, 0.488),
        ];
        let gyro_cases = [
            (GyroRange::Dps125, 4.375),
            (GyroRange::Dps250, 8.75),
            (GyroRange::Dps500, 17.5),
            (GyroRange::Dps1000, 35.0),
            (GyroRange::Dps2000, 70.0),
        ];
        for (accel_range, accel_mg) in accel_cases {
            for (gyro_range, gyro_mdps) in gyro_cases {
                let config = Config {
                    rate: Rate::Hz104,
                    accel_range,
                    gyro_range,
                };
                let mut bus = Bus::new();
                bus.push(1, 0, [1000, -1000, 0]);
                bus.push(2, 0, [1000, -1000, 0]);
                let mut sensor = Lsm6dso::open_configured(bus, 0x6a, Delay, config).unwrap();
                let mut sample = None;
                assert_eq!(
                    sensor.read_available(|value| {
                        sample = Some(value);
                        true
                    }),
                    Ok(Some(1))
                );
                let sample = sample.unwrap();
                let expected_accel = 1000.0 * accel_mg * 9.80665 / 1000.0;
                let expected_gyro = 1000.0 * gyro_mdps * core::f32::consts::PI / 180.0 / 1000.0;
                assert!((sample.accel_mps2[0] - expected_accel).abs() < 0.0001);
                assert!((sample.accel_mps2[1] + expected_accel).abs() < 0.0001);
                assert!((sample.gyro_rps[0] - expected_gyro).abs() < 0.00001);
                assert!((sample.gyro_rps[1] + expected_gyro).abs() < 0.00001);
                assert_eq!(sensor.bus.status_bursts, 1);
            }
        }
    }

    #[test]
    fn duplicate_source_mismatched_slots_and_unexpected_status_are_rejected() {
        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        sensor.bus.push(1, 0, [1, 0, 0]);
        sensor.bus.push(1, 0, [2, 0, 0]);
        assert_eq!(sensor.drain(|_| true), Err(Error::InvalidData));

        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        sensor.bus.push(1, 0, [1, 0, 0]);
        sensor.bus.push(2, 1, [2, 0, 0]);
        assert_eq!(sensor.drain(|_| true), Err(Error::InvalidData));

        for (flag, expected) in [
            (0x20, Error::Overflow),
            (0x08, Error::Overflow),
            (0x10, Error::InvalidData),
            (0x04, Error::InvalidData),
        ] {
            let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
            sensor.bus.status_flags = flag;
            assert_eq!(sensor.drain(|_| true), Err(expected));
        }
    }

    #[test]
    fn stop_disables_fifo_and_both_sensor_odrs() {
        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        peripheral::imu::Imu::stop(&mut sensor).unwrap();
        assert_eq!(sensor.bus.regs[FIFO_CTRL4 as usize] & 7, 0);
        assert_eq!(sensor.bus.regs[INT1_CTRL as usize] & 0x38, 0);
        assert_eq!(sensor.bus.regs[CTRL1_XL as usize] & 0xf0, 0);
        assert_eq!(sensor.bus.regs[CTRL2_G as usize] & 0xf0, 0);
    }

    #[test]
    fn stop_attempts_every_shutdown_write_after_a_bus_failure() {
        let mut sensor = Lsm6dso::open(Bus::new(), 0x6a, Delay).unwrap();
        sensor.bus.write_attempts.clear();
        sensor.bus.failed_write = Some(INT1_CTRL);
        assert_eq!(sensor.stop(), Err(Error::Bus));
        assert_eq!(
            &sensor.bus.write_attempts[..4],
            &[INT1_CTRL, FIFO_CTRL4, CTRL1_XL, CTRL2_G]
        );
        assert_eq!(sensor.bus.regs[FIFO_CTRL4 as usize] & 7, 0);
        assert_eq!(sensor.bus.regs[CTRL1_XL as usize] & 0xf0, 0);
        assert_eq!(sensor.bus.regs[CTRL2_G as usize] & 0xf0, 0);
    }
}

//! IMU sample contract; no hardware, OS, or runtime ownership.
#![forbid(unsafe_code)]

/// Failures reported by this capability's providers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Timeout,
    Full,
    Io,
    InvalidData,
}

/// Accelerometer full scale in multiples of standard gravity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AccelRange {
    G2,
    G4,
    G8,
    G16,
}

/// Gyroscope full scale in degrees per second.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GyroRange {
    Dps125,
    Dps250,
    Dps500,
    Dps1000,
    Dps2000,
}

/// Requirements for one paired accelerometer/gyroscope stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    /// Inclusive nominal rate limits in millihertz.
    pub min_rate_millihz: u32,
    pub max_rate_millihz: u32,
    pub accel_range: AccelRange,
    pub gyro_range: GyroRange,
    /// Maximum nominal time between delivery opportunities. Scheduling,
    /// bus transfer time and sink backpressure contribute additional latency.
    pub max_delivery_interval_us: u32,
}

/// Settings verified by the provider before acquisition starts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AppliedConfig {
    pub rate_millihz: u32,
    pub accel_range: AccelRange,
    pub gyro_range: GyroRange,
    /// Nominal interval between delivery opportunities in microseconds.
    pub delivery_interval_us: u32,
}

/// Modes supported by the selected provider with a one-sample delivery policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub rates_millihz: &'static [u32],
    pub accel_ranges: &'static [AccelRange],
    pub gyro_ranges: &'static [GyroRange],
}

impl Capabilities {
    /// Preview the slowest supported rate that meets all requirements.
    /// Zero rates or interval and reversed rate limits return `InvalidData`.
    /// Well-formed requirements without a matching mode return `Unsupported`.
    pub fn select(self, config: Config) -> Result<AppliedConfig, DeviceError> {
        let rates = config.min_rate_millihz..=config.max_rate_millihz;
        if config.min_rate_millihz == 0 || rates.is_empty() || config.max_delivery_interval_us == 0
        {
            return Err(DeviceError::InvalidData);
        }
        if !self.accel_ranges.contains(&config.accel_range)
            || !self.gyro_ranges.contains(&config.gyro_range)
        {
            return Err(DeviceError::Unsupported);
        }
        self.rates_millihz
            .iter()
            .copied()
            .filter(|rate| rates.contains(rate))
            .map(|rate_millihz| AppliedConfig {
                rate_millihz,
                accel_range: config.accel_range,
                gyro_range: config.gyro_range,
                delivery_interval_us: 1_000_000_000u32.div_ceil(rate_millihz),
            })
            .filter(|mode| mode.delivery_interval_us <= config.max_delivery_interval_us)
            .min_by_key(|mode| mode.rate_millihz)
            .ok_or(DeviceError::Unsupported)
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuSample {
    pub sequence: u64,
    /// Monotonic host reception time; shared by samples delivered in one batch.
    pub timestamp_ms: u64,
    /// Nominal hardware sampling interval, independent of delivery batching.
    pub sample_interval_us: u32,
    pub accel_mps2: [f32; 3],
    pub gyro_rps: [f32; 3],
}

/// Normalized task-context delivery. Faults also remain observable through join().
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Sample(ImuSample),
    Failed(DeviceError),
}

/// Owns acquisition execution. Request cancellation, then explicitly join.
/// Drop requests cancellation without blocking; the worker retains resources.
pub trait Imu {
    fn request_stop(&mut self) -> Result<(), DeviceError>;
    fn join(&mut self) -> Result<(), DeviceError>;
    fn is_finished(&self) -> bool;
    /// Events rejected because the destination was full, saturated at u32::MAX.
    fn dropped(&self) -> u32;
}

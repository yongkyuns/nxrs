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

/// Requirements for one paired accelerometer/gyroscope stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    /// Inclusive nominal rate limits in millihertz.
    pub min_rate_millihz: u32,
    pub max_rate_millihz: u32,
    /// Exact accelerometer full scale in multiples of standard gravity.
    pub accel_range_g: u16,
    /// Exact gyroscope full scale in degrees per second.
    pub gyro_range_dps: u16,
    /// Maximum nominal time between delivery opportunities. Scheduling,
    /// bus transfer time and sink backpressure contribute additional latency.
    pub max_delivery_interval_us: u32,
}

/// Accepted settings. `resolve` previews these without I/O; successful
/// `open_configured` also verifies the hardware configuration before acquisition.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AppliedConfig {
    pub rate_millihz: u32,
    /// Exact accelerometer full scale in multiples of standard gravity.
    pub accel_range_g: u16,
    /// Exact gyroscope full scale in degrees per second.
    pub gyro_range_dps: u16,
    /// Nominal interval between delivery opportunities in microseconds.
    pub delivery_interval_us: u32,
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

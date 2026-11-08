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

pub use peripheral::imu::{AppliedConfig, Config};

impl From<peripheral::imu::Error> for DeviceError {
    fn from(error: peripheral::imu::Error) -> Self {
        use peripheral::imu::Error;
        match error {
            Error::Unsupported => Self::Unsupported,
            Error::InvalidData => Self::InvalidData,
            Error::Io => Self::Io,
            Error::Timeout => Self::Timeout,
            Error::Overflow => Self::Full,
        }
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

//! IMU sample contract; no hardware, OS, or runtime ownership.
#![forbid(unsafe_code)]

pub use hal_common::DeviceError;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuSample {
    pub sequence: u64,
    pub timestamp_ms: u64,
    pub accel_mps2: [f32; 3],
    pub gyro_rps: [f32; 3],
}

pub trait Imu {
    /// Produce the sample available at the owner-selected monotonic timestamp.
    ///
    /// The caller decides when/how this method is scheduled. A real provider may
    /// instead return Busy until device readiness is signaled.
    fn sample(&mut self, now_ms: u64) -> Result<ImuSample, DeviceError>;
}

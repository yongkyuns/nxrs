//! GNSS fix contract; no receiver protocol, OS, or runtime ownership.
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

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GnssFix {
    pub sequence: u64,
    pub timestamp_ms: u64,
    pub north_m: f32,
    pub east_m: f32,
    pub speed_mps: f32,
    pub course_rad: f32,
}

pub trait Gnss {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError>;
}

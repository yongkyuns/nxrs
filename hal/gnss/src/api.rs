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
    /// WGS84 geodetic coordinates. Local map projection belongs to navigation.
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    /// Movement course, absent when the receiver cannot determine it.
    pub course_rad: Option<f32>,
}

pub trait Gnss {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError>;
}

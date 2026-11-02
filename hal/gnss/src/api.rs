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

/// Normalized task-context delivery. Faults also remain observable through join().
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Fix(GnssFix),
    /// Recoverable rejected wire frame; parsing continues at the next frame.
    InvalidFrame,
    Failed(DeviceError),
}

/// Owns acquisition execution. Request cancellation, then explicitly join.
/// Drop requests cancellation without blocking; the worker retains resources.
pub trait Gnss {
    fn request_stop(&mut self) -> Result<(), DeviceError>;
    fn join(&mut self) -> Result<(), DeviceError>;
    fn is_finished(&self) -> bool;
    /// Events rejected because the destination was full, saturated at u32::MAX.
    fn dropped(&self) -> u32;
}

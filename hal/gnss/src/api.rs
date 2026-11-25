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
    /// Microseconds on the monotonic device clock shared with other streams.
    pub timestamp_us: u64,
    /// True when aligned to the solution epoch. Otherwise this is UART reception time.
    pub is_capture_time: bool,
    /// Unix milliseconds at the solution epoch, when validated by the receiver.
    pub receiver_utc_ms: Option<i64>,
    /// WGS84 geodetic coordinates. Local map projection belongs to navigation.
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    /// Movement course, absent when the receiver cannot determine it.
    pub course_rad: Option<f32>,
    /// Receiver-provided accuracy and satellite data when available.
    pub quality: Option<FixQuality>,
}

/// Receiver estimates and satellite usage at the solution epoch.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FixQuality {
    pub satellites_used: u8,
    pub horizontal_accuracy_m: f32,
    pub vertical_accuracy_m: f32,
    pub speed_accuracy_mps: f32,
    /// Position dilution of precision (PDOP), a dimensionless factor.
    pub position_dop: Option<f32>,
    pub satellites: Option<SatelliteStatus>,
}

/// Summary of a recent satellite report.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SatelliteStatus {
    /// Satellites the receiver reports as visible or tracked.
    pub visible: u8,
    /// Satellites with a synchronized code or carrier lock.
    pub tracked: u8,
    /// Mean C/N₀ in dB-Hz among tracked satellites with a signal reading.
    pub mean_cn0_db_hz: Option<f32>,
    /// Milliseconds from the satellite report's receiver epoch to this fix.
    pub age_ms: u32,
}

/// Normalized task-context delivery. Faults also remain observable through join().
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Fix(GnssFix),
    /// Recoverable rejected wire frame; parsing continues at the next frame.
    InvalidFrame,
    Failed(DeviceError),
}

/// An active fix-stream lifetime, not the receiver's wire protocol. The provider
/// owns execution; this trait does not prescribe a thread or executor.
/// Request cancellation, then explicitly join.
/// Drop requests cancellation without blocking; the worker retains resources.
pub trait Session {
    fn request_stop(&mut self) -> Result<(), DeviceError>;
    fn join(&mut self) -> Result<(), DeviceError>;
    fn is_finished(&self) -> bool;
    /// Events rejected because the destination was full, saturated at u32::MAX.
    fn dropped(&self) -> u32;
}

/// A permanent configured receiver, separate from an active acquisition session.
pub trait Device: Sealed + Sync {
    fn id(&self) -> &'static str;
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        sink: S,
    ) -> Result<impl Session + Send + 'static, DeviceError>;
}
pub trait Sealed {}

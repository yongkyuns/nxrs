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
    /// Mean C/N₀ for tracked satellites with a reading, rounded to whole dB-Hz.
    pub mean_cn0_db_hz: Option<u8>,
    /// Milliseconds from the satellite report's receiver epoch to this fix.
    pub age_ms: u32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Constellation {
    Gps,
    Galileo,
    Beidou,
    Glonass,
    Sbas,
    Qzss,
    Navic,
    #[default]
    Other,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SatelliteSignal {
    pub constellation: Constellation,
    /// Satellite identifier within its constellation.
    pub id: u8,
    pub cn0_db_hz: u8,
}

/// Low-rate receiver report independent of position fixes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SatelliteReport {
    pub visible: u8,
    pub tracked: u8,
    pub mean_cn0_db_hz: Option<u8>,
    /// Nominal receiver report spacing. Zero means unknown.
    pub interval_ms: u32,
    /// Total number with measured signal strength, before limiting the list.
    pub measured: u8,
    signals: [SatelliteSignal; 24],
    len: u8,
}

impl SatelliteReport {
    pub fn new(
        visible: u8,
        tracked: u8,
        mean_cn0_db_hz: Option<u8>,
        interval_ms: u32,
        measured: u8,
        signals: impl IntoIterator<Item = SatelliteSignal>,
    ) -> Self {
        let mut report = Self {
            visible,
            tracked,
            mean_cn0_db_hz,
            interval_ms,
            measured,
            signals: [SatelliteSignal::default(); 24],
            len: 0,
        };
        for (slot, signal) in report.signals.iter_mut().zip(signals) {
            *slot = signal;
            report.len += 1;
        }
        report
    }

    /// Up to 24 strongest signals with a reported C/N₀.
    pub fn signals(&self) -> &[SatelliteSignal] {
        &self.signals[..usize::from(self.len)]
    }
}

/// Normalized task-context delivery. Faults also remain observable through join().
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Fix(GnssFix),
    Satellites(SatelliteReport),
    /// Recoverable rejected wire frame; parsing continues at the next frame.
    InvalidFrame,
    Failed(DeviceError),
}

/// An active receiver stream lifetime. The provider
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

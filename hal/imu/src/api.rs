//! Portable IMU samples, device access and acquisition contracts.
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

/// Independent terminal acquisition and cleanup evidence for a joined session.
/// Cleanup can fail after acquisition has already failed; neither cause erases
/// the other. This does not enumerate every individual cleanup operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Failure {
    pub acquisition: Option<DeviceError>,
    pub cleanup: Option<DeviceError>,
}

impl From<DeviceError> for Failure {
    fn from(error: DeviceError) -> Self {
        Self {
            acquisition: Some(error),
            cleanup: None,
        }
    }
}

impl Failure {
    pub(crate) fn reported_error(self) -> DeviceError {
        self.acquisition.or(self.cleanup).unwrap_or(DeviceError::Io)
    }

    pub(crate) fn joined_error(self) -> DeviceError {
        // Preserve the existing join contract: uncertain cleanup takes
        // precedence, while failure() keeps the acquisition cause as well.
        self.cleanup.or(self.acquisition).unwrap_or(DeviceError::Io)
    }
}

pub use peripheral::imu::{AppliedConfig, Config};

pub trait Sealed {}

/// A permanent, profile-selected device. References may be shared; acquisition
/// is exclusive. Construction and physical resources stay private to the HAL.
pub trait Device: Sealed + Sync {
    /// Canonical profile device ID; aliases return the same identity.
    fn id(&self) -> &'static str;
    fn default_config(&self) -> Result<Config, DeviceError>;
    fn resolve(&self, config: Config) -> Result<AppliedConfig, DeviceError>;
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        config: Config,
        sink: S,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError>;

    fn open_default<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        sink: S,
    ) -> Result<impl Session + Send + 'static, DeviceError> {
        self.open(self.default_config()?, sink)
            .map(|(session, _)| session)
    }
}

impl From<peripheral::imu::Error> for DeviceError {
    fn from(error: peripheral::imu::Error) -> Self {
        use peripheral::imu::Error;
        match error {
            Error::Unsupported => Self::Unsupported,
            Error::InvalidData => Self::InvalidData,
            Error::Io => Self::Io,
            Error::Timeout | Error::WouldBlock => Self::Timeout,
            Error::Overflow => Self::Full,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ImuSample {
    pub sequence: u64,
    /// Microseconds on the shared monotonic device clock.
    pub timestamp_us: u64,
    /// Delivery time on the same monotonic clock, before calling the sink.
    /// Capture age is available only when `is_capture_time` is true.
    pub received_us: u64,
    /// True for an estimated capture time. Otherwise the timestamp is reception time.
    pub is_capture_time: bool,
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

/// An active acquisition lifetime, distinct from `peripheral::imu::Imu` device
/// operations. The provider owns execution; this trait does not prescribe a
/// thread or executor. Request cancellation, then explicitly join.
/// Drop requests cancellation without blocking; the worker retains resources.
pub trait Session {
    fn request_stop(&mut self) -> Result<(), DeviceError>;
    /// Request cancellation and join. If this fails while `is_finished()` is
    /// false, retain the session and retry; the worker still owns its resources.
    fn join(&mut self) -> Result<(), DeviceError>;
    fn is_finished(&self) -> bool;
    /// Terminal detail retained after `join`, independently of event delivery.
    /// None means no retained detail (including a running/unjoined session or
    /// a provider that only implements the legacy single-error contract, or a
    /// worker panic whose failure phase is unknown).
    fn failure(&self) -> Option<Failure> {
        None
    }
    /// Events rejected because the destination was full, saturated at u32::MAX.
    fn dropped(&self) -> u32;
}

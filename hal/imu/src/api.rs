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

/// An active acquisition lifetime, distinct from `peripheral::imu::Imu` device
/// operations. The provider owns execution; this trait does not prescribe a
/// thread or executor. Request cancellation, then explicitly join.
/// Drop requests cancellation without blocking; the worker retains resources.
pub trait Session {
    fn request_stop(&mut self) -> Result<(), DeviceError>;
    fn join(&mut self) -> Result<(), DeviceError>;
    fn is_finished(&self) -> bool;
    /// Events rejected because the destination was full, saturated at u32::MAX.
    fn dropped(&self) -> u32;
}

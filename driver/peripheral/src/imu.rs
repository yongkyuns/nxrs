//! Paired accelerometer/gyroscope device operations, called in task context.

mod configuration;
pub use configuration::{select_rate, AppliedConfig, Config};

/// Failures of the shared IMU device protocol; ownership and delivery errors
/// belong to the consuming HAL.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    InvalidData,
    Io,
    Timeout,
    /// A drain could not make a coherent observation. No measurement was
    /// consumed or offered, and the caller can retry after a bounded wait.
    WouldBlock,
    Overflow,
}

/// Retention provided by the configured driver path, not the chip's marketing
/// capabilities. Neither mode guarantees lossless acquisition under overload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Buffering {
    /// Ordered hardware samples, with detected overflow reported as an error.
    Fifo,
    /// Only the latest available measurement. Intermediate samples may be lost
    /// without detection; a software queue cannot recover them.
    Latest,
}

/// Sensor-axis physical values with optional capture time on the caller's clock.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Measurement {
    pub capture_us: Option<u64>,
    pub accel_mps2: [f32; 3],
    pub gyro_rps: [f32; 3],
}

/// Implemented directly by a chip driver, alongside its chip-specific API.
/// Constructors remain concrete and accept the resources that driver requires.
/// Operations are synchronous and bounded by the reported hardware data; this
/// contract creates no worker, queue, ISR callback or wait for new samples.
pub trait Imu {
    const DEFAULT: Config;
    const BUFFERING: Buffering;
    type Settings;

    /// Resolve requirements without I/O. Only driver-supported combinations may
    /// succeed; concrete construction must apply and verify these settings.
    fn resolve(config: Config) -> Result<(Self::Settings, AppliedConfig), Error>;

    /// Read currently available complete measurements. A wake hint alone is not
    /// evidence of data. `Some(n)` counts offered measurements (zero is allowed);
    /// `None` means the consumer returned false and acquisition must stop.
    /// `now_us` supplies the same monotonic microsecond clock for every drain.
    /// Never invoke the consumer again after false. `Error::WouldBlock` defers
    /// the drain before consuming any data. Other errors may follow an emitted
    /// prefix and must not imply that sample continuity was retained.
    fn read_available(
        &mut self,
        now_us: impl FnMut() -> u64,
        emit: impl FnMut(Measurement) -> bool,
    ) -> Result<Option<usize>, Error>;

    /// Disable sampling and sensor interrupt output before resources are freed.
    fn stop(&mut self) -> Result<(), Error>;
}

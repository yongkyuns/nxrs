//! Synchronous GNSS receiver protocol, without acquisition or host timestamps.
use std::time::Instant;

pub mod aiding;

/// Accuracy estimates and satellite usage at the solution epoch.
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

/// Summary of a receiver's recent satellite report.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SatelliteStatus {
    /// Satellites the receiver reports as visible or tracked.
    pub visible: u8,
    /// Satellites with a synchronized code or carrier lock.
    pub tracked: u8,
    /// Mean carrier-to-noise density, rounded to whole dB-Hz.
    pub mean_cn0_db_hz: Option<u8>,
    /// Milliseconds from the satellite report's receiver epoch to this fix.
    pub age_ms: u32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
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
    pub id: u8,
    pub cn0_db_hz: u8,
}

pub struct SatelliteReport<'a> {
    pub status: SatelliteStatus,
    pub interval_ms: u32,
    pub measured: u8,
    pub signals: &'a [SatelliteSignal],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Io,
    Timeout,
    InvalidData,
    NoFix,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Measurement {
    /// Receiver epoch in milliseconds; used to suppress repeated navigation reports.
    pub epoch_ms: u64,
    /// Unix milliseconds from the receiver when its UTC date and time are valid.
    pub receiver_utc_ms: Option<i64>,
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    pub course_rad: Option<f32>,
    pub quality: Option<FixQuality>,
}

/// Time of week of the next pulse. The receiver can use either time grid.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TimePulse {
    Gps(u32),
    Utc(u32),
}

pub enum Report<'a> {
    Fix(Measurement),
    /// A checked report, available while searching for a fix as well.
    Satellites(SatelliteReport<'a>),
    /// None invalidates the timing association, for example on loss of lock.
    Pulse(Option<TimePulse>),
}

/// Bounded, synchronous operations on an already opened byte stream.
/// Failed configuration must retain enough state for `restore` to roll back.
pub trait Receiver {
    fn aiding_capabilities(&self) -> aiding::Capabilities {
        aiding::Capabilities::default()
    }
    /// Admit one hint without waiting for the transport. `now_us` uses the
    /// same caller-supplied monotonic clock as the hint's observation.
    fn begin_aiding(
        &mut self,
        _: aiding::Aid,
        _: u64,
        _: Instant,
    ) -> Result<(), aiding::SubmitError> {
        Err(aiding::SubmitError::Unsupported)
    }
    /// At most one bounded write; incoming replies are demultiplexed by
    /// read_available. Complete is returned once. No nested receive/wait loop.
    fn advance_aiding(&mut self, _: Instant, _: u64) -> Result<aiding::Progress, Error> {
        Ok(aiding::Progress::Idle)
    }
    /// Retire the operation. An incomplete frame that cannot be safely
    /// completed is a transport error, not an ordinary aiding rejection.
    fn cancel_aiding(&mut self) -> Result<(), Error> {
        Ok(())
    }
    fn configure(&mut self, deadline: Instant) -> Result<(), Error>;
    fn enable_time_pulse(&mut self, _: Instant) -> Result<(), Error> {
        Err(Error::InvalidData)
    }
    fn restore(&mut self, deadline: Instant) -> Result<(), Error>;
    /// Read at most one bounded chunk. Invalid frames are recoverable events;
    /// transport failures terminate the call. NoFix is not a corrupt frame.
    fn read_available(&mut self, emit: impl FnMut(Result<Report<'_>, Error>)) -> Result<(), Error>;
}

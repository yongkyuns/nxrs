//! Synchronous GNSS receiver protocol, without acquisition or host timestamps.
use std::time::Instant;

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
    pub latitude_deg: f64,
    pub longitude_deg: f64,
    pub speed_mps: f32,
    pub course_rad: Option<f32>,
}

/// Bounded, synchronous operations on an already opened byte stream.
/// Failed configuration must retain enough state for `restore` to roll back.
pub trait Receiver {
    fn configure(&mut self, deadline: Instant) -> Result<(), Error>;
    fn restore(&mut self, deadline: Instant) -> Result<(), Error>;
    /// Read at most one bounded chunk. Invalid frames are recoverable events;
    /// transport failures terminate the call. NoFix is not a corrupt frame.
    fn read_available(&mut self, emit: impl FnMut(Result<Measurement, Error>))
        -> Result<(), Error>;
}

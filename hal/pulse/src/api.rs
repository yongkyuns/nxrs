#![forbid(unsafe_code)]
use std::{fmt, time::Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    InvalidConfig,
    Io,
    /// Cleanup or cancellation failed; success and resource release are unproven.
    Shutdown,
    Closed,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Unsupported => "pulse capability is unavailable",
            Self::Busy => "pulse resources are already owned",
            Self::InvalidConfig => "invalid pulse configuration",
            Self::Io => "pulse I/O failed",
            Self::Shutdown => "pulse cleanup failed",
            Self::Closed => "pulse session is closed",
        })
    }
}
impl std::error::Error for Error {}

/// Requested waveform, admitted at 10..=100 Hz and 1..=99 percent duty.
/// These limits bound this capability's intended observation workload. They
/// are not an accuracy guarantee: the selected timer quantizes the waveform,
/// and notification delivery can coalesce edges. Use Session::disable for off.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Waveform {
    frequency_hz: u32,
    duty_percent: u8,
}

impl Waveform {
    /// Validation does not acquire resources or change output.
    pub fn new(frequency_hz: u32, duty_percent: u8) -> Result<Self, Error> {
        if !(10..=100).contains(&frequency_hz) || !(1..=99).contains(&duty_percent) {
            return Err(Error::InvalidConfig);
        }
        Ok(Self {
            frequency_hz,
            duty_percent,
        })
    }
    pub fn frequency_hz(self) -> u32 {
        self.frequency_hz
    }
    pub fn duty_percent(self) -> u8 {
        self.duty_percent
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    /// Sequence of observed notifications, starting at one. received is the
    /// worker's monotonic receipt time, not an ISR or physical edge timestamp.
    Edge {
        sequence: u64,
        received: Instant,
    },
    Fault(Error),
}

/// Saturating counts for this session. rejected counts edge callbacks returning
/// false, not missed physical pulses. Snapshots can include an in-flight callback.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub observed: u64,
    pub rejected: u64,
    /// Retained independently of whether a terminal Fault event was admitted.
    pub fault: Option<Error>,
}

/// Owns one configured output and its notification worker.
///
/// The sink runs only on that worker in task context. It must return promptly,
/// never wait for queue space, and must not panic. A false result rejects that
/// event; it is never retried. Faults also remain in stats and shutdown results.
///
/// Explicit shutdown stops output, requests cancellation, joins the source and
/// retires the sink before returning success. A failed cancellation retains the
/// worker for retry; a failed output stop retains the output for retry. Dropping
/// a session requests stop and attempts output-off but does not hide a join.
/// Resources remain reserved until the worker and sink actually retire. A native
/// cleanup failure can quarantine the selected resources until reboot.
pub trait Session: Send + 'static {
    fn configure(&mut self, waveform: Waveform) -> Result<(), Error>;
    /// Stop output; already queued/in-flight edge notifications may still arrive.
    fn disable(&mut self) -> Result<(), Error>;
    fn stats(&self) -> Stats;
    /// Idempotent after completion. A source fault remains an error even after
    /// successful resource cleanup. No hard shutdown deadline is promised.
    fn shutdown(&mut self) -> Result<Stats, Error>;
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn waveform_accepts_only_the_documented_domain() {
        for frequency in [10, 25, 100] {
            for duty in [1, 50, 99] {
                let waveform = Waveform::new(frequency, duty).unwrap();
                assert_eq!(
                    (waveform.frequency_hz(), waveform.duty_percent()),
                    (frequency, duty)
                );
            }
        }
        for (frequency, duty) in [
            (0, 50),
            (1, 50),
            (9, 50),
            (101, 50),
            (u32::MAX, 50),
            (25, 0),
            (25, 100),
            (25, u8::MAX),
        ] {
            assert_eq!(Waveform::new(frequency, duty), Err(Error::InvalidConfig));
        }
    }
}

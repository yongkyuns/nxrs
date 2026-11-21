//! Optional task-context acquisition timing and bus transaction observers.
//! Observers must not block; the caller owns acquisition and cleanup policy.
use std::time::Instant;

#[cfg(not(feature = "probe"))]
mod disabled;
#[cfg(feature = "probe")]
mod enabled;

#[cfg(not(feature = "probe"))]
pub use disabled::{Observed, Trace};
#[cfg(feature = "probe")]
pub use enabled::{Observed, Trace};

#[derive(Clone, Copy, Debug)]
pub enum Timing {
    /// The caller finished waiting for readiness and acknowledging its source.
    Wake {
        at: Instant,
        periodic_deadline: Option<Instant>,
    },
    /// Synchronous I²C/SPI transaction interval, including adapter and kernel wait.
    Ioctl { start: Instant, end: Instant },
    /// A batch drain returned, including decoding and delivery. Failure or early
    /// termination sets `complete` to false.
    Drain { at: Instant, complete: bool },
}

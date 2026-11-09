//! Timing observations for the `probe` feature. The device catalogue owns
//! diagnostic acquisition; the HAL owns these acquisition event semantics.
//! Observer callbacks run in task context and must not block.
use std::time::Instant;

#[derive(Clone, Copy, Debug)]
pub enum Timing {
    /// Userspace has finished waiting for readiness and acknowledging GPIO.
    Wake {
        at: Instant,
        periodic_deadline: Option<Instant>,
    },
    /// Synchronous I²C transaction interval, including adapter checks and kernel wait.
    Ioctl { start: Instant, end: Instant },
    /// FIFO drain returned, including decoding and sample delivery. A failure
    /// or early termination sets `complete` to false.
    Drain { at: Instant, complete: bool },
}

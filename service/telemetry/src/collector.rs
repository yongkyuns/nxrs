//! Bounded summary delivery on a caller-supplied nonblocking stream.
//! The caller owns connecting, waiting, reconnecting and the next service turn.
use crate::{publication::Publisher, wire, Snapshot};
use std::io;
use std::time::{Duration, Instant};
use text::Text;

#[cfg(feature = "collector-tls")]
mod tls;
#[cfg(feature = "collector-tls")]
pub use tls::Upload;

/// Longest admitted summary interval.
pub const MAX_REPORT_INTERVAL: Duration = Duration::from_secs(86_400);
const WRITE_TIMEOUT: Duration = Duration::from_secs(3);

/// One bounded summary frame and its reporting allowance, independent of the
/// transport lifecycle. Keep this value across failed connection attempts.
pub struct Reports {
    interval: Duration,
    next: Instant,
    publication: Publisher<Snapshot, Text<{ wire::CELLULAR_FRAME_BYTES }>>,
}

impl Reports {
    /// Start with one report due immediately. Zero, intervals longer than a day,
    /// and unrepresentable deadlines are rejected before the snapshot is read.
    pub fn new(interval: Duration, now: Instant) -> io::Result<Self> {
        if interval.is_zero()
            || interval > MAX_REPORT_INTERVAL
            || now.checked_add(interval).is_none()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "collector report interval must be greater than zero and at most one day",
            ));
        }
        Ok(Self {
            interval,
            next: now,
            publication: Publisher::default(),
        })
    }

    /// Retire a failed stream's partial frame without refreshing its allowance.
    /// Call only after discarding that stream; its accepted prefix is incomplete.
    pub fn reset_stream(&mut self) {
        self.publication.reset_stream();
    }

    /// Earliest time at which a new summary may be sampled. Pending bytes can
    /// need earlier turns; the transport's readiness/wait belongs to the caller.
    pub fn next_report(&self) -> Instant {
        self.next
    }

    /// Whether a frame or its flush is still in progress on the current stream.
    pub fn in_flight(&self) -> bool {
        self.publication.in_flight()
    }

    /// Perform at most one write and one flush. Snapshot production runs only
    /// when a new report is due. Missed intervals do not accumulate a burst.
    /// The stream must be nonblocking; a frame expires after three seconds.
    /// On error discard the stream, then call `reset_stream` before replacement.
    /// Returns true only when a whole frame and its flush have completed.
    pub fn service(
        &mut self,
        output: &mut impl io::Write,
        now: Instant,
        snapshot: impl FnOnce() -> Snapshot,
    ) -> io::Result<bool> {
        if !self.publication.in_flight() && now >= self.next {
            // A failed or partial attempt spends this interval too. Replacing
            // the TLS stream does not refresh the allowance.
            self.next = now + self.interval;
            self.publication.offer(snapshot());
        }
        self.publication
            .service(output, now, now + WRITE_TIMEOUT, wire::cellular_frame)
    }
}

//! NuttX acquisition and process-global scan lease.
#![deny(unsafe_op_in_unsafe_fn)]

#[cfg(not(test))]
use crate::worker::Worker;
use crate::Error;
#[cfg(not(test))]
use crate::{Event, EventSink};
use std::sync::atomic::{AtomicBool, Ordering};

mod native;
#[cfg(test)]
mod tests;

static ACTIVE: AtomicBool = AtomicBool::new(false);

/// Native callback storage is shared; the worker retains this until quiet.
#[derive(Debug)]
struct Lease;
impl Lease {
    fn acquire() -> Result<Self, Error> {
        ACTIVE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Self)
            .map_err(|_| Error::Busy)
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        ACTIVE.store(false, Ordering::Release);
    }
}

#[cfg(not(test))]
pub(crate) fn open<S: EventSink<Event> + Send + 'static>(sink: S) -> Result<Worker, Error> {
    Worker::start(native::Native::new(), sink, Lease::acquire()?)
}

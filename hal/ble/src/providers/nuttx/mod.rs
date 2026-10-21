//! NuttX acquisition and process-global scan lease.
#![deny(unsafe_op_in_unsafe_fn)]

use crate::worker::Lease;
#[cfg(not(test))]
use crate::worker::Worker;
#[cfg(not(test))]
use crate::Error;
#[cfg(not(test))]
use crate::{Event, EventSink};

mod native;
#[cfg_attr(test, allow(dead_code))]
mod peers;
#[cfg(test)]
mod tests;

#[cfg(not(test))]
pub(crate) fn open<S: EventSink<Event> + Send + 'static>(sink: S) -> Result<Worker, Error> {
    Worker::start_with_stack(native::Native::new(), sink, Lease::acquire()?, 8192)
}

#[cfg(not(test))]
mod gatt;

//! Shared session lifetime and accounting; providers own device operations.
use crate::{Error, Event, Session, Stats, Waveform};
use acquisition::{Wake, Worker};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicU8, Ordering},
        Arc, Mutex, MutexGuard,
    },
    time::Instant,
};

pub(crate) trait Output: Send + 'static {
    fn configure(&mut self, waveform: Waveform) -> Result<(), Error>;
    fn disable(&mut self) -> Result<(), Error>;
    /// Retain native uncertainty if the owner abandons failed output cleanup.
    fn quarantine(&mut self) {}
}

pub(crate) trait Input: Send + 'static {
    /// Wait for a notification or cancellation, never for consumer queue space.
    fn next(&mut self) -> Result<Option<Instant>, Error>;
    fn finish(&mut self) -> Result<(), Error>;
}

pub(crate) struct Admission(AtomicU8);
impl Admission {
    pub const fn new() -> Self {
        Self(AtomicU8::new(0))
    }
    pub fn acquire(&'static self) -> Result<Arc<Lease>, Error> {
        match self
            .0
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => Ok(Arc::new(Lease(self))),
            Err(1) => Err(Error::Busy),
            Err(_) => Err(Error::Shutdown),
        }
    }
}
pub(crate) struct Lease(&'static Admission);
impl Lease {
    pub fn quarantine(&self) {
        self.0 .0.store(2, Ordering::Release);
    }
    pub fn is_quarantined(&self) -> bool {
        self.0 .0.load(Ordering::Acquire) == 2
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        // A failed native teardown leaves admission quarantined.
        let _ = self
            .0
             .0
            .compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

pub(crate) struct Managed<O: Output, C: Wake> {
    // Output is retired before cancellation storage; the source retains its
    // own resource lease through cleanup and sink destruction.
    output: Option<O>,
    worker: Option<Worker<C, Error>>,
    stats: Arc<Mutex<Stats>>,
    closing: bool,
    completed: Option<Result<Stats, Error>>,
}

pub(crate) fn start<O, I, C, G, F>(
    output: O,
    input: I,
    cancel: C,
    guard: G,
    stack_bytes: usize,
    sink: F,
) -> Result<Managed<O, C>, Error>
where
    O: Output,
    I: Input,
    C: Wake,
    G: Send + 'static,
    F: FnMut(Event) -> bool + Send + 'static,
{
    let stats = Arc::new(Mutex::new(Stats::default()));
    let source_stats = stats.clone();
    // Retire the input and sink before releasing the lease, including when
    // the thread cannot start or the callback unwinds.
    let mut source = (input, sink, guard);
    let worker = Worker::start("pulse-input", stack_bytes, cancel, (), move |stop, _| {
        let (input, sink, _guard) = &mut source;
        let run = collect(input, stop, &source_stats, sink);
        let result = input.finish().map_err(|_| Error::Shutdown).and(run);
        if let Err(error) = result {
            locked(&source_stats).fault = Some(error);
            if !stop.load(Ordering::Acquire) {
                sink(Event::Fault(error));
            }
        }
        result
    })
    .map_err(|_| Error::Io)?;
    Ok(Managed {
        output: Some(output),
        worker: Some(worker),
        stats,
        closing: false,
        completed: None,
    })
}

fn collect(
    input: &mut impl Input,
    stop: &AtomicBool,
    stats: &Mutex<Stats>,
    sink: &mut impl FnMut(Event) -> bool,
) -> Result<(), Error> {
    while !stop.load(Ordering::Acquire) {
        let received = match input.next()? {
            Some(received) => received,
            None if stop.load(Ordering::Acquire) => break,
            None => return Err(Error::Io),
        };
        if stop.load(Ordering::Acquire) {
            break;
        }
        let sequence = {
            let mut stats = locked(stats);
            stats.observed = stats.observed.saturating_add(1);
            stats.observed
        };
        if !sink(Event::Edge { sequence, received }) {
            let mut stats = locked(stats);
            stats.rejected = stats.rejected.saturating_add(1);
        }
    }
    Ok(())
}

impl<O: Output, C: Wake> Session for Managed<O, C> {
    fn configure(&mut self, waveform: Waveform) -> Result<(), Error> {
        if self.closing {
            return Err(Error::Closed);
        }
        if let Some(error) = self.stats().fault {
            return Err(error);
        }
        self.output
            .as_mut()
            .ok_or(Error::Closed)?
            .configure(waveform)
    }
    fn disable(&mut self) -> Result<(), Error> {
        self.output.as_mut().ok_or(Error::Closed)?.disable()
    }
    fn stats(&self) -> Stats {
        let mut stats = locked(&self.stats);
        // An unexpected worker exit (including a sink panic) is unavailable,
        // never a silently healthy output. Normal cancellation sets closing.
        if !self.closing
            && self.worker.as_ref().is_some_and(Worker::is_finished)
            && stats.fault.is_none()
        {
            stats.fault = Some(Error::Io);
        }
        *stats
    }
    fn shutdown(&mut self) -> Result<Stats, Error> {
        if let Some(result) = self.completed {
            return result;
        }
        self.closing = true;
        // Attempt both cleanup paths even when stopping output fails.
        let output = self.output.as_mut().map_or(Ok(()), Output::disable);
        let source = self
            .worker
            .as_mut()
            .map_or(Ok(()), |worker| worker.stop_and_join(Error::Shutdown));
        if output.is_err()
            || self
                .worker
                .as_ref()
                .is_some_and(|worker| !worker.is_finished())
        {
            locked(&self.stats).fault = Some(Error::Shutdown);
            // Keep output and any running worker for a subsequent cleanup retry.
            return Err(Error::Shutdown);
        }
        if let Err(error) = source {
            let mut stats = locked(&self.stats);
            if stats.fault.is_none() {
                stats.fault = Some(error);
            }
        }
        self.worker.take();
        self.output.take();
        let stats = *locked(&self.stats);
        let result = stats.fault.map_or(Ok(stats), Err);
        self.completed = Some(result);
        result
    }
}

impl<O: Output, C: Wake> Drop for Managed<O, C> {
    fn drop(&mut self) {
        if let Some(output) = self.output.as_mut() {
            if output.disable().is_err() {
                output.quarantine();
            }
        }
        // No join in Drop. The acquisition worker retains its input, sink and
        // guard until it actually leaves the blocking wait and finishes cleanup.
        if let Some(worker) = &self.worker {
            let _ = worker.request_stop();
        }
    }
}

#[cfg(test)]
mod tests;

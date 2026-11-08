//! Explicit mock-only timer source. Production providers wait on device signals.
use crate::{Wake, Worker};
use std::{
    io,
    sync::{atomic::Ordering, Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

#[derive(Clone, Default)]
struct Cancel(Arc<(Mutex<bool>, Condvar)>);
impl Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        let (lock, changed) = &*self.0;
        *lock
            .lock()
            .map_err(|_| io::Error::other("mock cancellation"))? = true;
        changed.notify_one();
        Ok(())
    }
}
pub struct Session<E: Copy> {
    worker: Worker<Cancel, E>,
    io_error: E,
}
impl<E: Copy + Send + 'static> Session<E> {
    pub fn start<
        T: Send + 'static,
        S: FnMut(T) -> messaging::DeliveryResult<T> + Send + 'static,
    >(
        sink: S,
        period: Duration,
        io_error: E,
        failure: fn(E) -> T,
        mut next: impl FnMut(u64) -> T + Send + 'static,
    ) -> Result<Self, E> {
        let cancel = Cancel::default();
        let wait = cancel.clone();
        Worker::start(
            "sensor-mock",
            16384,
            cancel,
            sink,
            failure,
            move |stop, emit| {
                let origin = Instant::now();
                let mut deadline = origin + period;
                while !stop.load(Ordering::Acquire) {
                    let (lock, changed) = &*wait.0;
                    let stopped = lock.lock().map_err(|_| io_error)?;
                    let (stopped, _) = changed
                        .wait_timeout_while(
                            stopped,
                            deadline.saturating_duration_since(Instant::now()),
                            |stopped| !*stopped,
                        )
                        .map_err(|_| io_error)?;
                    if *stopped {
                        break;
                    }
                    drop(stopped);
                    if !emit(next(
                        origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
                    )) {
                        break;
                    }
                    deadline = Instant::now() + period;
                }
                Ok(())
            },
        )
        .map(|worker| Self { worker, io_error })
        .map_err(|_| io_error)
    }
    pub fn request_stop(&self) -> Result<(), E> {
        self.worker.request_stop().map_err(|_| self.io_error)
    }
    pub fn join(&mut self) -> Result<(), E> {
        self.request_stop()?;
        self.worker.join(self.io_error)
    }
    pub fn is_finished(&self) -> bool {
        self.worker.is_finished()
    }
    pub fn dropped(&self) -> u32 {
        self.worker.dropped()
    }
}

//! Explicit mock-only timer source. Production providers wait on device signals.
use crate::acquisition::{Wake, Worker};
use crate::{DeviceError, Event};
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
pub(crate) struct Session(Worker<Cancel, DeviceError>);
impl Session {
    pub(crate) fn start<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        sink: S,
        period: Duration,
        mut next: impl FnMut(u64) -> Event + Send + 'static,
    ) -> Result<Self, DeviceError> {
        let cancel = Cancel::default();
        let wait = cancel.clone();
        Worker::start(
            "sensor-mock",
            16384,
            cancel,
            sink,
            Event::Failed,
            move |stop, emit| {
                let origin = Instant::now();
                let mut deadline = origin + period;
                while !stop.load(Ordering::Acquire) {
                    let (lock, changed) = &*wait.0;
                    let stopped = lock.lock().map_err(|_| DeviceError::Io)?;
                    let (stopped, _) = changed
                        .wait_timeout_while(
                            stopped,
                            deadline.saturating_duration_since(Instant::now()),
                            |stopped| !*stopped,
                        )
                        .map_err(|_| DeviceError::Io)?;
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
        .map(Self)
        .map_err(|_| DeviceError::Io)
    }
    pub(crate) fn request_stop(&self) -> Result<(), DeviceError> {
        self.0.request_stop().map_err(|_| DeviceError::Io)
    }
    pub(crate) fn join(&mut self) -> Result<(), DeviceError> {
        self.request_stop()?;
        self.0.join(DeviceError::Io)
    }
    pub(crate) fn is_finished(&self) -> bool {
        self.0.is_finished()
    }
    pub(crate) fn dropped(&self) -> u32 {
        self.0.dropped()
    }
}

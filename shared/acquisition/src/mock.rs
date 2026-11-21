//! Explicit mock-only timer source. Production providers wait on device signals.
use crate::Wake;
use std::{
    io,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    time::{Duration, Instant},
};

#[derive(Clone, Default)]
pub struct Timer(Arc<(Mutex<bool>, Condvar)>);
impl Wake for Timer {
    fn wake(&self) -> io::Result<()> {
        let (lock, changed) = &*self.0;
        *lock
            .lock()
            .map_err(|_| io::Error::other("mock cancellation"))? = true;
        changed.notify_one();
        Ok(())
    }
}
impl Timer {
    pub fn run(
        &self,
        stop: &AtomicBool,
        period: Duration,
        mut tick: impl FnMut(u64) -> bool,
    ) -> io::Result<()> {
        let origin = Instant::now();
        let mut deadline = origin + period;
        while !stop.load(Ordering::Acquire) {
            let (lock, changed) = &*self.0;
            let stopped = lock
                .lock()
                .map_err(|_| io::Error::other("mock cancellation"))?;
            let (stopped, _) = changed
                .wait_timeout_while(
                    stopped,
                    deadline.saturating_duration_since(Instant::now()),
                    |stopped| !*stopped,
                )
                .map_err(|_| io::Error::other("mock cancellation"))?;
            if *stopped {
                break;
            }
            drop(stopped);
            if !tick(origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX)) {
                break;
            }
            deadline = Instant::now() + period;
        }
        Ok(())
    }
}

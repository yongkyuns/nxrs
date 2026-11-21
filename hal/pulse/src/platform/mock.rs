//! Explicit timer-driven observation fixture, not a native hardware provider.
use crate::{
    runtime::{self, Admission, Input, Lease, Output},
    Error, Event, Session, Waveform,
};
use acquisition::Wake;
use std::{
    io,
    sync::{Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

static ADMISSION: Admission = Admission::new();

#[derive(Default)]
struct Control {
    waveform: Option<Waveform>,
    generation: u64,
    stopped: bool,
}
type Shared = Arc<(Mutex<Control>, Condvar)>;

struct MockOutput {
    shared: Shared,
    lease: Arc<Lease>,
}
struct MockInput {
    shared: Shared,
    _lease: Arc<Lease>,
}
struct Cancel(Shared);

impl Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        let (state, changed) = &*self.0;
        state
            .lock()
            .map_err(|_| io::Error::other("pulse mock cancellation"))?
            .stopped = true;
        changed.notify_one();
        Ok(())
    }
}

impl Output for MockOutput {
    fn configure(&mut self, waveform: Waveform) -> Result<(), Error> {
        if self.lease.is_quarantined() {
            return Err(Error::Shutdown);
        }
        let (state, changed) = &*self.shared;
        let mut state = state.lock().map_err(|_| Error::Io)?;
        if state.stopped {
            return Err(Error::Closed);
        }
        state.waveform = Some(waveform);
        state.generation = state.generation.wrapping_add(1);
        changed.notify_one();
        Ok(())
    }
    fn disable(&mut self) -> Result<(), Error> {
        let (state, changed) = &*self.shared;
        let mut state = state.lock().map_err(|_| Error::Io)?;
        state.waveform = None;
        state.generation = state.generation.wrapping_add(1);
        changed.notify_one();
        Ok(())
    }
    fn quarantine(&mut self) {
        self.lease.quarantine();
    }
}

impl Input for MockInput {
    fn next(&mut self) -> Result<Option<Instant>, Error> {
        let (lock, changed) = &*self.shared;
        let mut state = lock.lock().map_err(|_| Error::Io)?;
        loop {
            if state.stopped {
                return Ok(None);
            }
            let Some(waveform) = state.waveform else {
                state = changed.wait(state).map_err(|_| Error::Io)?;
                continue;
            };
            let generation = state.generation;
            let deadline = Instant::now()
                + Duration::from_nanos(1_000_000_000 / u64::from(waveform.frequency_hz()));
            loop {
                let (new_state, _) = changed
                    .wait_timeout(state, deadline.saturating_duration_since(Instant::now()))
                    .map_err(|_| Error::Io)?;
                state = new_state;
                if state.stopped {
                    return Ok(None);
                }
                if state.generation != generation {
                    break;
                }
                if Instant::now() >= deadline {
                    return Ok(Some(Instant::now()));
                }
            }
        }
    }
    fn finish(&mut self) -> Result<(), Error> {
        Ok(())
    }
}

pub fn open<F: FnMut(Event) -> bool + Send + 'static>(sink: F) -> Result<impl Session, Error> {
    let lease = ADMISSION.acquire()?;
    let shared = Arc::new((Mutex::new(Control::default()), Condvar::new()));
    runtime::start(
        MockOutput {
            shared: shared.clone(),
            lease: lease.clone(),
        },
        MockInput {
            shared: shared.clone(),
            _lease: lease.clone(),
        },
        Cancel(shared),
        lease,
        16384,
        sink,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn explicit_mock_is_exclusive_counts_rejection_and_can_reopen_after_join() {
        let (sent, received) = mpsc::channel();
        let mut session = open(move |event| {
            let _ = sent.send(event);
            false
        })
        .unwrap();
        assert!(matches!(open(|_| true), Err(Error::Busy)));
        assert_eq!(session.stats(), crate::Stats::default());
        session.configure(Waveform::new(100, 50).unwrap()).unwrap();
        for sequence in [1, 2] {
            assert!(matches!(
                received.recv_timeout(Duration::from_secs(5)).unwrap(),
                Event::Edge { sequence: observed, .. } if observed == sequence
            ));
        }
        let stats = session.shutdown().unwrap();
        assert!(stats.observed >= 2);
        assert_eq!(stats.rejected, stats.observed);
        assert_eq!(stats.fault, None);
        while received.try_recv().is_ok() {}
        assert_eq!(received.try_recv(), Err(mpsc::TryRecvError::Disconnected));
        let mut reopened = open(|_| true).unwrap();
        assert_eq!(reopened.shutdown().unwrap(), crate::Stats::default());
    }
}

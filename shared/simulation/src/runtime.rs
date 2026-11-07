//! One wall-paced clock for the ordinary host simulation profile.
use crate::{CancelHandle, EndpointLease, Error, Event, Simulation, SimulationSpec, Source};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, Weak,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

static ACTIVE: Mutex<Weak<Clock>> = Mutex::new(Weak::new());

struct Clock {
    seed: u64,
    sources: &'static [Source],
    session: Simulation,
    stopped: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Clock {
    fn start(seed: u64, sources: &'static [Source]) -> Result<Self, AcquireError> {
        let mut spec = SimulationSpec::coordinated(seed);
        spec.sources = sources.iter().copied().collect();
        let step = sources
            .iter()
            .map(|source| match source {
                Source::Imu => spec.imu_period_us,
                Source::Gnss => spec.gnss_period_us,
            })
            .min()
            .ok_or(AcquireError::Engine(Error::InvalidSpec))?;
        let interval = Duration::from_micros(step);
        let session = Simulation::new(spec).map_err(AcquireError::Engine)?;
        let stopped = Arc::new(AtomicBool::new(false));
        let worker_session = session.clone();
        let worker_stopped = stopped.clone();
        let thread = thread::Builder::new()
            .name("simulation-clock".into())
            .spawn(move || {
                if worker_session.start().is_err() {
                    return;
                }
                let mut now = 0u64;
                let mut deadline = Instant::now() + interval;
                while !worker_stopped.load(Ordering::Acquire) {
                    let wait = deadline.saturating_duration_since(Instant::now());
                    if !wait.is_zero() {
                        thread::park_timeout(wait);
                        continue;
                    }
                    let Some(next) = now.checked_add(step) else {
                        worker_session.cancel();
                        return;
                    };
                    if worker_session.advance_to(next).is_err() {
                        worker_session.cancel();
                        return;
                    }
                    now = next;
                    deadline = Instant::now() + interval;
                }
            })
            .map_err(|_| AcquireError::Thread)?;
        Ok(Self {
            seed,
            sources,
            session,
            stopped,
            thread: Some(thread),
        })
    }
}

impl Drop for Clock {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        self.session.cancel();
        if let Some(thread) = self.thread.take() {
            thread.thread().unpark();
            // The clock worker owns simulation state without a reference to Clock.
            let _ = thread.join();
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AcquireError {
    Engine(Error),
    ConflictingSpec,
    Thread,
}

pub struct LiveEndpoint {
    lease: EndpointLease,
    _clock: Arc<Clock>,
}

impl LiveEndpoint {
    pub fn cancel_handle(&self) -> CancelHandle {
        self.lease.cancel_handle()
    }

    pub fn recv(&self) -> Result<Event, Error> {
        self.lease.recv()
    }
}

pub fn acquire(
    seed: u64,
    sources: &'static [Source],
    source: Source,
) -> Result<LiveEndpoint, AcquireError> {
    let mut active = ACTIVE.lock().map_err(|_| AcquireError::Thread)?;
    let clock = if let Some(clock) = active.upgrade() {
        if clock.seed != seed || clock.sources != sources {
            return Err(AcquireError::ConflictingSpec);
        }
        clock
    } else {
        let clock = Arc::new(Clock::start(seed, sources)?);
        *active = Arc::downgrade(&clock);
        clock
    };
    let lease = clock.session.lease(source).map_err(AcquireError::Engine)?;
    Ok(LiveEndpoint {
        lease,
        _clock: clock,
    })
}

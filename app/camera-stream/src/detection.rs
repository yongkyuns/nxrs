use camera::Frame;
use detection_service::{
    Error, Event, Monitor, Observation, Policy, Presence, Worker, WorkerStats,
};

const EVENTS: usize = 8;
#[derive(Clone, Copy, Debug)]
pub struct SecurityEvent {
    pub id: u64,
    pub at_ms: u64,
    pub event: Event,
}
#[derive(Clone, Copy, Debug)]
pub struct DetectionSnapshot {
    pub presence: Presence,
    pub latest: Option<Observation>,
    pub worker: WorkerStats,
    pub failures: u64,
    pub event_count: u64,
    pub events: [Option<SecurityEvent>; EVENTS],
}

/// Product composition of inference and alert policy, exposed as typed Rust
/// state. No network listener, footage persistence or notification is implicit.
/// The application retains at most eight local events; consumers detect gaps
/// through monotonically increasing IDs and the total event_count.
pub struct SecurityDetection {
    worker: Worker,
    monitor: Monitor,
    events: [Option<SecurityEvent>; EVENTS],
    event_count: u64,
    failures: u64,
    stopped: bool,
    worker_failed: bool,
}
impl SecurityDetection {
    pub fn new(worker: Worker, policy: Policy) -> Result<Self, Error> {
        let monitor = Monitor::new(policy)?;
        Ok(Self {
            worker,
            monitor,
            events: [None; EVENTS],
            event_count: 0,
            failures: 0,
            stopped: false,
            worker_failed: false,
        })
    }
    pub fn snapshot(&self) -> DetectionSnapshot {
        DetectionSnapshot {
            presence: self.monitor.presence(),
            latest: self.monitor.latest(),
            worker: self.worker.stats(),
            failures: self.failures,
            event_count: self.event_count,
            events: self.events,
        }
    }
    fn event(&mut self, event: Option<Event>, now_ms: u64) {
        if let Some(event) = event {
            self.event_count = self.event_count.saturating_add(1);
            self.events[((self.event_count - 1) % EVENTS as u64) as usize] = Some(SecurityEvent {
                id: self.event_count,
                at_ms: now_ms,
                event,
            });
        }
    }
    fn failed(&mut self, error: Error, now_ms: u64) {
        self.failures = self.failures.saturating_add(1);
        self.worker_failed |= matches!(error, Error::Worker | Error::Stopped);
        let event = self.monitor.unavailable();
        self.event(event, now_ms);
    }
    pub(crate) fn poll(&mut self, now_ms: u64, frame: Option<Frame<'_>>) {
        if self.stopped {
            return;
        }
        if !self.worker_failed {
            match self.worker.poll() {
                Ok(Some(observation)) => match self.monitor.observe(observation, now_ms) {
                    Ok(event) => self.event(event, now_ms),
                    Err(error) => self.failed(error, now_ms),
                },
                Ok(None) => {}
                Err(error) => self.failed(error, now_ms),
            }
            if let Some(frame) = frame {
                if !self.worker_failed {
                    if let Err(error) = self.worker.offer(frame, now_ms) {
                        self.failed(error, now_ms);
                    }
                }
            }
        }
        match self.monitor.tick(now_ms) {
            Ok(event) => self.event(event, now_ms),
            Err(error) => self.failed(error, now_ms),
        }
    }
    pub fn stop(&mut self) -> Result<(), Error> {
        let result = self.worker.stop();
        if result.is_err() {
            self.failures = self.failures.saturating_add(1);
        }
        self.monitor.unavailable();
        self.stopped = true;
        result
    }

    pub(crate) fn stopped(&self) -> bool {
        self.stopped
    }
}

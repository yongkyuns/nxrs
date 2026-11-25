use super::{publication::Publication, worker::Worker};
use super::{Error, Input, Monitor, Observation, Policy, WorkerOptions, WorkerStats};
use crate::{Event, EventKind, Presence, EVENT_HISTORY};
use detector::Detector;

#[derive(Clone, Copy, Debug)]
pub struct Snapshot {
    pub presence: Presence,
    pub latest: Option<Observation>,
    pub worker: WorkerStats,
    pub failures: u64,
    pub event_count: u64,
    pub events: [Option<Event>; EVENT_HISTORY],
}

/// Owns the inference worker, alert policy, event history and publication for
/// one detection lifetime. The caller drives polling and delivers metadata.
pub struct Session {
    worker: Worker,
    monitor: Monitor,
    events: [Option<Event>; EVENT_HISTORY],
    event_count: u64,
    failures: u64,
    publication: Publication,
}
impl Session {
    pub fn start<D: Detector + 'static>(
        options: WorkerOptions,
        policy: Policy,
        initialize: impl FnOnce() -> Result<D, Error> + Send + 'static,
    ) -> Result<Self, Error> {
        let mut monitor = Monitor::new(policy)?;
        let publication = Publication::new()?;
        let worker = Worker::spawn(options, initialize)?;
        monitor.localization = worker.capabilities().localization;
        Ok(Self {
            worker,
            monitor,
            events: [None; EVENT_HISTORY],
            event_count: 0,
            failures: 0,
            publication,
        })
    }

    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            presence: self.monitor.presence(),
            latest: self.monitor.latest(),
            worker: self.worker.stats(),
            failures: self.failures,
            event_count: self.event_count,
            events: self.events,
        }
    }

    /// The current publication ID and encoded bytes, borrowed until the next poll.
    #[cfg(feature = "wire")]
    pub fn publication(&self) -> Option<(u64, &[u8])> {
        if self.stopped() {
            return None;
        }
        self.publication.metadata()
    }

    fn event(&mut self, event: Option<EventKind>, now_ms: u64) {
        if let Some(kind) = event {
            self.event_count = self.event_count.saturating_add(1);
            self.events[((self.event_count - 1) % EVENT_HISTORY as u64) as usize] = Some(Event {
                id: self.event_count,
                at_ms: now_ms,
                kind,
            });
        }
    }
    fn failed(&mut self, now_ms: u64) {
        self.failures = self.failures.saturating_add(1);
        let event = self.monitor.unavailable();
        self.event(event, now_ms);
    }

    /// Advance detection deadlines even when capture has no new frame.
    pub fn poll(&mut self, now_ms: u64, input: Option<Input<'_>>) {
        if self.stopped() {
            return;
        }
        if !self.worker.failed() {
            match self.worker.poll() {
                Ok(Some(observation)) => match self.monitor.observe(observation, now_ms) {
                    Ok(event) => self.event(event, now_ms),
                    Err(_) => self.failed(now_ms),
                },
                Ok(None) => {}
                Err(_) => self.failed(now_ms),
            }
            if !self.worker.failed()
                && input.is_some_and(|input| self.worker.offer(input, now_ms).is_err())
            {
                self.failed(now_ms);
            }
        }
        match self.monitor.tick(now_ms) {
            Ok(event) => self.event(event, now_ms),
            Err(_) => self.failed(now_ms),
        }
        let snapshot = self.snapshot();
        self.publication
            .update(&snapshot, self.worker.capabilities().localization, now_ms);
    }
    pub fn stop(&mut self) -> Result<(), Error> {
        let result = self.worker.stop();
        if result.is_err() {
            self.failures = self.failures.saturating_add(1);
        }
        self.monitor.unavailable();
        result
    }

    pub fn stopped(&self) -> bool {
        self.worker.stopped()
    }
}

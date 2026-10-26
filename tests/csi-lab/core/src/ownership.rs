//! One existing control task owns the calibration model. Native source callbacks
//! only transfer bounded owned features through a single-producer handoff.
//! Consumer/control work never owns the producer gate or waits in a callback.
use crate::calibration::{Calibration, Config, Feature, Rejection, Snapshot};
use crate::journal::{self, ModelEvent, Operation};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, TryLockError};
use std::time::{Duration, Instant};

#[derive(Clone, Copy)]
pub struct Source {
    pub feature: Feature,
    pub window: u32,
}
struct Slot {
    generation: AtomicU32,
    value: Mutex<Option<Source>>,
}
struct Shared {
    accepting: AtomicBool,
    publishing: AtomicBool,
    tail: AtomicU32,
    slots: Box<[Slot]>,
}
#[derive(Clone)]
pub struct Ingress(Arc<Shared>);
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Error {
    Configuration,
    Closed,
    SourceAdmission,
    Pending,
    Poisoned,
}
/// SDK callbacks must handle admission failure; the supplied failure operation
/// is an atomic sticky latch, never a model mutation or blocking notification.
impl Ingress {
    pub const fn storage_bytes(capacity: usize) -> usize {
        capacity * std::mem::size_of::<Slot>() + std::mem::size_of::<Shared>()
    }
    #[inline(never)]
    pub fn observe(
        &self,
        feature: Feature,
        window: u32,
        fail: impl FnOnce(Rejection),
    ) -> Result<(), Error> {
        self.publish(Source { feature, window }, fail, || {})
    }
    fn publish(
        &self,
        source: Source,
        fail: impl FnOnce(Rejection),
        after_reserve: impl FnOnce(),
    ) -> Result<(), Error> {
        if !self.0.accepting.load(Ordering::SeqCst) {
            return Err(Error::Closed);
        }
        if self
            .0
            .publishing
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            fail(Rejection::SourceContention);
            return Err(Error::SourceAdmission);
        }
        struct Publishing<'a>(&'a AtomicBool);
        impl Drop for Publishing<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::SeqCst);
            }
        }
        let _active = Publishing(&self.0.publishing);
        if !self.0.accepting.load(Ordering::SeqCst) {
            return Err(Error::Closed);
        }
        let serial = self.0.tail.load(Ordering::Relaxed);
        let slot = &self.0.slots[serial as usize % self.0.slots.len()];
        // No ticket is consumed on overload. Only the single source publisher
        // advances tail; consumer/control operations never touch its gate.
        if serial > u32::MAX / 2 - 256 || slot.generation.load(Ordering::Acquire) != 2 * serial {
            fail(Rejection::SourceContention);
            return Err(Error::SourceAdmission);
        }
        let mut value = match slot.value.try_lock() {
            Ok(value) => value,
            Err(TryLockError::Poisoned(_)) => {
                fail(Rejection::Poisoned);
                return Err(Error::Poisoned);
            }
            Err(TryLockError::WouldBlock) => {
                fail(Rejection::SourceContention);
                return Err(Error::SourceAdmission);
            }
        };
        self.0.tail.store(serial + 1, Ordering::Release);
        after_reserve();
        *value = Some(source);
        drop(value);
        slot.generation.store(2 * serial + 1, Ordering::Release);
        Ok(())
    }
}

#[derive(Clone, Copy)]
pub enum Marker {
    Label(u32),
    Training {
        command: u32,
        status: i32,
        wander: f32,
        jitter: f32,
    },
}
#[derive(Clone, Copy)]
pub enum Evidence {
    Model(ModelEvent),
    Marker { time_us: u64, marker: Marker },
}
/// Concrete adapters provide one monotonic model clock, the existing first-fault
/// latch, and a nonblocking owned evidence sink. No SDK object enters this core.
pub trait Environment {
    fn now_us(&self) -> u64;
    fn failure(&self) -> Rejection;
    fn fail(&self, reason: Rejection);
    fn record(&self, evidence: Evidence);
}
pub struct Control<E: Environment> {
    ingress: Ingress,
    next: u32,
    value: Calibration,
    environment: E,
    wait: Duration,
    closed: bool,
}
impl<E: Environment> Control<E> {
    pub fn new(
        config: Config,
        capacity: usize,
        wait: Duration,
        environment: E,
    ) -> Result<(Self, Ingress), Error> {
        if capacity == 0 || capacity > 256 || wait.is_zero() || wait > Duration::from_secs(1) {
            return Err(Error::Configuration);
        }
        let value = Calibration::new(config).ok_or(Error::Configuration)?;
        let slots = (0..capacity)
            .map(|i| Slot {
                generation: AtomicU32::new(2 * i as u32),
                value: Mutex::new(None),
            })
            .collect::<Vec<_>>()
            .into_boxed_slice();
        // Force lazy target mutex creation before the SDK source is admitted.
        for slot in &slots {
            drop(slot.value.lock().map_err(|_| Error::Poisoned)?);
        }
        let ingress = Ingress(Arc::new(Shared {
            accepting: AtomicBool::new(true),
            publishing: AtomicBool::new(false),
            tail: AtomicU32::new(0),
            slots,
        }));
        Ok((
            Self {
                ingress: ingress.clone(),
                next: 0,
                value,
                environment,
                wait,
                closed: false,
            },
            ingress,
        ))
    }
    fn execute(&mut self, operation: Operation, window: u32) -> (bool, Snapshot) {
        // Only this owner executes the policy, samples its clock, or journals
        // model checkpoints. A paused sink cannot exclude a source publisher.
        let now = self.environment.now_us();
        let before = self.environment.failure();
        if before != Rejection::None {
            self.value.reject(before);
        }
        let accepted = operation.apply(&mut self.value, now);
        let after = self.environment.failure();
        if after != Rejection::None {
            self.value.reject(after);
        }
        let snapshot = self.value.checkpoint();
        let mut event = journal::model_event(operation, now, before, after, accepted, snapshot);
        event.words[23] = window;
        self.environment.record(Evidence::Model(event));
        (accepted, snapshot)
    }
    fn take(&mut self) -> Result<Option<Source>, Error> {
        let slot = &self.ingress.0.slots[self.next as usize % self.ingress.0.slots.len()];
        if slot.generation.load(Ordering::Acquire) != 2 * self.next + 1 {
            return Ok(None);
        }
        let mut value = slot.value.try_lock().map_err(|_| Error::Poisoned)?;
        let source = value.take().ok_or(Error::Poisoned)?;
        drop(value);
        slot.generation.store(
            2 * (self.next + self.ingress.0.slots.len() as u32),
            Ordering::Release,
        );
        self.next += 1;
        Ok(Some(source))
    }
    fn through(&mut self, barrier: u32, deadline: Option<Instant>) -> Result<(), Error> {
        while self.next < barrier {
            match self.take() {
                Ok(Some(source)) => {
                    self.execute(Operation::Observe(source.feature), source.window);
                }
                Ok(None) => {
                    let Some(deadline) = deadline else {
                        return Err(Error::Pending);
                    };
                    if Instant::now() >= deadline {
                        return Err(Error::Pending);
                    }
                    // Never spin behind a preempted producer on a fixed-priority OS.
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(error) => {
                    self.environment.fail(Rejection::Poisoned);
                    return Err(error);
                }
            }
        }
        Ok(())
    }
    /// Service only the source prefix already reserved at entry, bounding work
    /// even when callbacks keep arriving. An unpublished slot returns promptly.
    pub fn drain_ready(&mut self) -> Result<(), Error> {
        if self.closed {
            return Err(Error::Closed);
        }
        let barrier = self.ingress.0.tail.load(Ordering::Acquire);
        self.through(barrier, None)
    }
    fn barrier(&mut self) -> Result<(), Error> {
        if self.closed {
            return Err(Error::Closed);
        }
        let barrier = self.ingress.0.tail.load(Ordering::Acquire);
        self.through(barrier, Some(Instant::now() + self.wait))
    }
    pub fn operation(&mut self, operation: Operation) -> Result<(bool, Snapshot), Error> {
        // Source submissions use ingress; never synchronously mutate the model
        // through this control interface or manufacture an unrecorded window ID.
        if matches!(operation, Operation::Observe(_)) {
            return Err(Error::Configuration);
        }
        if let Err(error) = self.barrier() {
            if error != Error::Closed && !matches!(operation, Operation::Poll) {
                self.environment.fail(Rejection::ControlContention);
            }
            return Err(error);
        }
        let (accepted, snapshot) = self.execute(operation, 0);
        // A real admission/native failure can arrive while the evidence sink
        // is preempted. Journal a fault poll before exposing a response rather
        // than presenting its earlier clear snapshot after the sticky latch.
        let failure = self.environment.failure();
        if failure != Rejection::None && snapshot.rejection != failure {
            return Ok((accepted, self.execute(Operation::Poll, 0).1));
        }
        Ok((accepted, snapshot))
    }
    pub fn marker(&mut self, marker: Marker) -> Result<(), Error> {
        match marker {
            Marker::Label(n) if n > 3 => return Err(Error::Configuration),
            Marker::Training { command, .. } if !(1..=2).contains(&command) => {
                return Err(Error::Configuration)
            }
            _ => {}
        }
        self.barrier().inspect_err(|error| {
            if *error != Error::Closed {
                self.environment.fail(Rejection::ControlContention);
            }
        })?;
        self.environment.record(Evidence::Marker {
            time_us: self.environment.now_us(),
            marker,
        });
        Ok(())
    }
    pub fn counts(&self) -> (u32, u32) {
        (self.ingress.0.tail.load(Ordering::Acquire), self.next)
    }
    pub fn pending(&self) -> u32 {
        self.ingress.0.tail.load(Ordering::Acquire) - self.next
    }
    pub fn close(&mut self) -> Result<(), Error> {
        self.ingress.0.accepting.store(false, Ordering::SeqCst);
        self.closed = true;
        let deadline = Instant::now() + self.wait;
        while self.ingress.0.publishing.load(Ordering::SeqCst) {
            if Instant::now() >= deadline {
                self.environment.fail(Rejection::ControlContention);
                return Err(Error::Pending);
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        let barrier = self.ingress.0.tail.load(Ordering::Acquire);
        self.through(barrier, Some(deadline))
    }
}
impl<E: Environment> Drop for Control<E> {
    fn drop(&mut self) {
        self.ingress.0.accepting.store(false, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calibration::{Phase, Runtime, State};
    use std::sync::{atomic::AtomicU64, Barrier};
    #[derive(Clone)]
    struct Hooks {
        fault: Arc<Runtime>,
        clock: Arc<AtomicU64>,
        records: Arc<Mutex<Vec<Evidence>>>,
        pause: Option<Arc<(Barrier, Barrier, AtomicBool)>>,
    }
    impl Hooks {
        fn new() -> Self {
            let fault = Arc::new(Runtime::new());
            assert!(fault.prepare(config()));
            Self {
                fault,
                clock: Arc::new(AtomicU64::new(0)),
                records: Arc::new(Mutex::new(Vec::new())),
                pause: None,
            }
        }
        fn at(&self, now: u64) {
            self.clock.store(now, Ordering::Relaxed);
        }
    }
    impl Environment for Hooks {
        fn now_us(&self) -> u64 {
            self.clock.load(Ordering::Relaxed)
        }
        fn failure(&self) -> Rejection {
            self.fault.failure_reason()
        }
        fn fail(&self, r: Rejection) {
            self.fault.fail(r);
        }
        fn record(&self, e: Evidence) {
            if matches!(e,Evidence::Model(v) if v.words[0]==5) {
                if let Some(p) = &self.pause {
                    if p.2.swap(false, Ordering::SeqCst) {
                        p.0.wait();
                        p.1.wait();
                    }
                }
            }
            self.records.lock().unwrap().push(e);
        }
    }
    fn config() -> Config {
        Config {
            minimum_features: 25,
            minimum_duration_us: 1000,
            ..Config::EMPTY_HOLDOUT
        }
    }
    fn feature(n: u32, now: u64) -> Feature {
        Feature {
            sequence: n,
            oldest_us: now,
            newest_us: now,
            templates: 10,
            training_complete: true,
            gain_available: true,
            wander: 0.01,
        }
    }
    fn ready(h: &Hooks) -> (Control<Hooks>, Ingress) {
        let (mut owner, p) =
            Control::new(config(), 64, Duration::from_millis(10), h.clone()).unwrap();
        assert!(owner.operation(Operation::Start(10)).unwrap().0);
        for n in 1..=50 {
            h.at(u64::from(n) * 1000);
            p.observe(feature(n, u64::from(n) * 1000), n, |r| h.fail(r))
                .unwrap();
            owner.drain_ready().unwrap();
        }
        h.at(51000);
        assert!(owner.operation(Operation::Validate).unwrap().0);
        for n in 51..=100 {
            h.at(u64::from(n) * 1000 + 1000);
            p.observe(feature(n, u64::from(n) * 1000 + 1000), n, |r| h.fail(r))
                .unwrap();
            owner.drain_ready().unwrap();
        }
        h.at(102000);
        assert!(owner.operation(Operation::Accept).unwrap().0);
        h.at(103000);
        p.observe(feature(101, 103000), 101, |r| h.fail(r)).unwrap();
        owner.drain_ready().unwrap();
        assert_eq!(
            owner.operation(Operation::Poll).unwrap().1.state,
            State::NotDetected
        );
        (owner, p)
    }
    fn verify(h: &Hooks) {
        let mut model = Calibration::new(config()).unwrap();
        for e in h.records.lock().unwrap().iter() {
            if let Evidence::Model(v) = e {
                journal::replay_model(&mut model, &v.to_record(), true).unwrap();
            }
        }
    }
    #[test]
    fn paused_poll_cannot_exclude_source_and_all_checkpoints_replay_exactly() {
        let mut h = Hooks::new();
        let gate = Arc::new((Barrier::new(2), Barrier::new(2), AtomicBool::new(false)));
        h.pause = Some(gate.clone());
        let (mut owner, p) = ready(&h);
        h.at(104000);
        gate.2.store(true, Ordering::SeqCst);
        std::thread::scope(|scope| {
            let reader = scope.spawn(move || {
                let response = owner.operation(Operation::Poll).unwrap();
                (owner, response)
            });
            gate.0.wait();
            h.at(105000);
            p.observe(feature(102, 105000), 102, |r| h.fail(r)).unwrap();
            assert_eq!(h.failure(), Rejection::None);
            gate.1.wait();
            let (mut owner, response) = reader.join().unwrap();
            assert_eq!(response.1.state, State::NotDetected);
            owner.drain_ready().unwrap();
            assert_eq!(
                owner.operation(Operation::Poll).unwrap().1.phase,
                Phase::Ready
            );
            assert_eq!(owner.counts(), (102, 102));
            owner.close().unwrap();
        });
        verify(&h);
    }
    #[test]
    fn genuine_source_overload_is_sticky_and_response_cannot_remain_clear() {
        let mut h = Hooks::new();
        let gate = Arc::new((Barrier::new(2), Barrier::new(2), AtomicBool::new(false)));
        h.pause = Some(gate.clone());
        let (mut owner, p) = ready(&h);
        gate.2.store(true, Ordering::SeqCst);
        std::thread::scope(|scope| {
            let reader = scope.spawn(move || {
                let result = owner.operation(Operation::Poll).unwrap();
                (owner, result)
            });
            gate.0.wait();
            for n in 102..166 {
                p.observe(feature(n, 103000), n, |r| h.fail(r)).unwrap();
            }
            assert_eq!(
                p.observe(feature(166, 103000), 166, |r| h.fail(r)),
                Err(Error::SourceAdmission)
            );
            gate.1.wait();
            let (mut owner, response) = reader.join().unwrap();
            assert_eq!(response.1.phase, Phase::Rejected);
            assert_eq!(response.1.rejection, Rejection::SourceContention);
            assert_eq!(response.1.state, State::Unknown);
            owner.drain_ready().unwrap();
            assert_eq!(owner.pending(), 0);
            owner.close().unwrap();
        });
        verify(&h);
    }
    #[test]
    fn control_and_labels_follow_all_reserved_source_events_without_relabelling_them() {
        let h = Hooks::new();
        let (mut owner, p) =
            Control::new(config(), 8, Duration::from_millis(5), h.clone()).unwrap();
        owner.operation(Operation::Start(10)).unwrap();
        h.at(100);
        for n in 1..=3 {
            p.observe(feature(n, u64::from(n)), n, |r| h.fail(r))
                .unwrap();
        }
        owner.marker(Marker::Label(2)).unwrap();
        let events = h.records.lock().unwrap();
        assert_eq!(events.len(), 5);
        for (i, e) in events[1..4].iter().enumerate() {
            let Evidence::Model(v) = e else { panic!() };
            assert_eq!(v.words[23], i as u32 + 1);
        }
        assert!(matches!(
            events[4],
            Evidence::Marker {
                marker: Marker::Label(2),
                ..
            }
        ));
        drop(events);
        verify(&h);
    }
    #[test]
    fn preempted_source_is_not_spun_on_and_busy_poll_does_not_poison_the_model() {
        let h = Hooks::new();
        let (mut owner, p) =
            Control::new(config(), 2, Duration::from_millis(2), h.clone()).unwrap();
        let gate = (Barrier::new(2), Barrier::new(2));
        std::thread::scope(|scope| {
            let source = scope.spawn(|| {
                p.publish(
                    Source {
                        feature: feature(1, 0),
                        window: 1,
                    },
                    |r| h.fail(r),
                    || {
                        gate.0.wait();
                        gate.1.wait();
                    },
                )
            });
            gate.0.wait();
            assert_eq!(owner.drain_ready(), Err(Error::Pending));
            assert!(matches!(
                owner.operation(Operation::Poll),
                Err(Error::Pending)
            ));
            assert_eq!(h.failure(), Rejection::None);
            assert!(h.records.lock().unwrap().is_empty());
            assert!(matches!(
                owner.operation(Operation::Validate),
                Err(Error::Pending)
            ));
            assert_eq!(h.failure(), Rejection::ControlContention);
            gate.1.wait();
            source.join().unwrap().unwrap();
            owner.drain_ready().unwrap();
            assert_eq!(h.records.lock().unwrap().len(), 1);
            owner.close().unwrap();
        });
        verify(&h);
    }
    #[test]
    fn shutdown_waits_for_owned_publication_drains_and_rejects_late_callbacks() {
        let h = Hooks::new();
        let (mut owner, p) = Control::new(config(), 2, Duration::from_secs(1), h.clone()).unwrap();
        let gate = (Barrier::new(2), Barrier::new(2));
        std::thread::scope(|scope| {
            let source = scope.spawn(|| {
                p.publish(
                    Source {
                        feature: feature(1, 0),
                        window: 7,
                    },
                    |r| h.fail(r),
                    || {
                        gate.0.wait();
                        gate.1.wait();
                    },
                )
            });
            gate.0.wait();
            let finish = scope.spawn(move || {
                owner.close().unwrap();
                owner
            });
            while p.0.accepting.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
            assert_eq!(
                p.observe(feature(2, 0), 8, |_| panic!("late epoch is not overflow")),
                Err(Error::Closed)
            );
            gate.1.wait();
            source.join().unwrap().unwrap();
            let mut owner = finish.join().unwrap();
            assert_eq!(owner.counts(), (1, 1));
            assert!(matches!(
                owner.operation(Operation::Poll),
                Err(Error::Closed)
            ));
        });
        verify(&h);
    }
    #[test]
    fn no_radio_progress_still_expires_and_native_failure_stays_unavailable() {
        let h = Hooks::new();
        let (mut owner, _) = ready(&h);
        h.at(1_103_001);
        assert_eq!(
            owner.operation(Operation::Poll).unwrap().1.rejection,
            Rejection::ProgressExpired
        );
        verify(&h);
        let h = Hooks::new();
        let (mut owner, _) = ready(&h);
        h.fail(Rejection::NativeError);
        let response = owner.operation(Operation::Poll).unwrap().1;
        assert_eq!(response.rejection, Rejection::NativeError);
        assert_eq!(response.state, State::Unknown);
        verify(&h);
    }
    #[test]
    fn failed_shutdown_retains_inflight_owned_storage_and_never_reopens_epoch() {
        let h = Hooks::new();
        let (mut owner, p) =
            Control::new(config(), 2, Duration::from_millis(2), h.clone()).unwrap();
        let gate = (Barrier::new(2), Barrier::new(2));
        std::thread::scope(|scope| {
            let source = scope.spawn(|| {
                p.publish(
                    Source {
                        feature: feature(1, 0),
                        window: 1,
                    },
                    |r| h.fail(r),
                    || {
                        gate.0.wait();
                        gate.1.wait();
                    },
                )
            });
            gate.0.wait();
            assert_eq!(owner.close(), Err(Error::Pending));
            assert_eq!(h.failure(), Rejection::ControlContention);
            drop(owner); // Ingress keeps the owned queue alive; no SDK pointer is stored.
            assert_eq!(
                p.observe(feature(2, 0), 2, |_| panic!()),
                Err(Error::Closed)
            );
            gate.1.wait();
            source.join().unwrap().unwrap();
            assert!(p.0.slots[0].value.lock().unwrap().is_some());
        });
        assert!(h.records.lock().unwrap().is_empty());
    }
    #[test]
    fn producer_identity_contention_and_drop_do_not_wait_or_reset_failures() {
        let h = Hooks::new();
        let (owner, p) = Control::new(config(), 1, Duration::from_millis(5), h.clone()).unwrap();
        let gate = (Barrier::new(2), Barrier::new(2));
        std::thread::scope(|scope| {
            let source = scope.spawn(|| {
                p.publish(
                    Source {
                        feature: feature(1, 0),
                        window: 1,
                    },
                    |r| h.fail(r),
                    || {
                        gate.0.wait();
                        gate.1.wait();
                    },
                )
            });
            gate.0.wait();
            assert_eq!(
                p.observe(feature(2, 0), 2, |r| h.fail(r)),
                Err(Error::SourceAdmission)
            );
            gate.1.wait();
            source.join().unwrap().unwrap();
        });
        assert_eq!(h.failure(), Rejection::SourceContention);
        drop(owner);
        assert_eq!(
            p.observe(feature(3, 0), 3, |_| panic!()),
            Err(Error::Closed)
        );
    }
}

#[cfg(test)]
mod admission_tests {
    use super::*;
    use crate::calibration::{Phase, Runtime};
    use std::sync::atomic::AtomicU64;
    #[derive(Clone)]
    struct Hooks {
        clock: Arc<AtomicU64>,
        failure: Arc<Runtime>,
        events: Arc<Mutex<Vec<ModelEvent>>>,
    }
    impl Environment for Hooks {
        fn now_us(&self) -> u64 {
            self.clock.load(Ordering::Relaxed)
        }
        fn failure(&self) -> Rejection {
            self.failure.failure_reason()
        }
        fn fail(&self, r: Rejection) {
            self.failure.fail(r)
        }
        fn record(&self, e: Evidence) {
            if let Evidence::Model(v) = e {
                self.events.lock().unwrap().push(v);
            }
        }
    }
    fn setup() -> (Control<Hooks>, Ingress, Hooks) {
        let r = Arc::new(Runtime::new());
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        let h = Hooks {
            clock: Arc::new(AtomicU64::new(0)),
            failure: r,
            events: Arc::new(Mutex::new(Vec::new())),
        };
        let config = Config {
            minimum_features: 25,
            minimum_duration_us: 1000,
            ..Config::EMPTY_HOLDOUT
        };
        let (owner, p) = Control::new(config, 8, Duration::from_millis(5), h.clone()).unwrap();
        (owner, p, h)
    }
    fn feature(n: u32, now: u64) -> Feature {
        Feature {
            sequence: n,
            oldest_us: now,
            newest_us: now,
            templates: 10,
            training_complete: true,
            gain_available: true,
            wander: 0.01,
        }
    }
    #[test]
    fn owned_copy_retains_source_identity_and_source_clock_errors_are_not_masked() {
        let (mut owner, p, h) = setup();
        owner.operation(Operation::Start(10)).unwrap();
        h.clock.store(100, Ordering::Relaxed);
        let mut f = feature(1, 100);
        p.observe(f, 77, |r| h.fail(r)).unwrap();
        f.wander = 0.9;
        owner.drain_ready().unwrap();
        let rows = h.events.lock().unwrap();
        let last = rows.last().unwrap();
        assert_eq!(last.words[21], 0.01f32.to_bits());
        assert_eq!(last.words[23], 77);
        drop(rows);
        // Two callbacks with the same identity are a real source reversal.
        p.observe(f, 78, |r| h.fail(r)).unwrap();
        owner.drain_ready().unwrap();
        assert_eq!(
            owner.operation(Operation::Poll).unwrap().1.rejection,
            Rejection::SourceReversed
        );
        assert_eq!(h.failure(), Rejection::None); // Algorithm rejection, not injected fault.
    }
    #[test]
    fn model_clock_rollback_remains_an_algorithm_rejection() {
        let (mut owner, _, h) = setup();
        owner.operation(Operation::Start(10)).unwrap();
        h.clock.store(100, Ordering::Relaxed);
        owner.operation(Operation::Poll).unwrap();
        h.clock.store(99, Ordering::Relaxed);
        assert_eq!(
            owner.operation(Operation::Poll).unwrap().1.rejection,
            Rejection::ClockRollback
        );
    }
    #[test]
    fn queue_delay_cannot_turn_expired_rf_into_valid_background_data() {
        let (mut owner, p, h) = setup();
        owner.operation(Operation::Start(10)).unwrap();
        h.clock.store(900000, Ordering::Relaxed);
        p.observe(feature(1, 900000), 1, |r| h.fail(r)).unwrap();
        owner.drain_ready().unwrap();
        let mut stale = feature(2, 900001);
        stale.oldest_us = 0;
        p.observe(stale, 2, |r| h.fail(r)).unwrap();
        h.clock.store(1_000_001, Ordering::Relaxed);
        owner.drain_ready().unwrap();
        let state = owner.operation(Operation::Poll).unwrap().1;
        assert_eq!(state.phase, Phase::Rejected);
        assert_eq!(state.rejection, Rejection::RfExpired);
    }
    #[test]
    fn malformed_controls_and_closed_owner_do_not_execute_late_mutations() {
        let (mut owner, _, h) = setup();
        assert_eq!(owner.marker(Marker::Label(4)), Err(Error::Configuration));
        assert!(matches!(
            owner.operation(Operation::Observe(feature(1, 0))),
            Err(Error::Configuration)
        ));
        assert!(h.events.lock().unwrap().is_empty());
        owner.close().unwrap();
        assert!(matches!(
            owner.operation(Operation::Start(10)),
            Err(Error::Closed)
        ));
        assert_eq!(owner.marker(Marker::Label(1)), Err(Error::Closed));
        assert_eq!(h.failure(), Rejection::None);
    }
}

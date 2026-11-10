//! Finite task-context contention, separate from sustained offered-load tests.
//!
//! One or four producers share one class queue. Every attempt is retained in
//! preallocated correlation storage; the payload remains the actual measured
//! type. The owner requests a 10 ms receive timeout to revisit completion or
//! cancellation after a last rejected attempt without a data-path sentinel.
//! This cannot bound a reserved-slot spin before the channel's deadline check;
//! the independent watchdog and external capture deadline contain that failure.
use crate::{
    native::{self, Gate, Result, Schedule},
    payload::Payload,
    report::{Report, Sample},
    support::{
        self, Clock, Config, OBSERVER_PRIORITY, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS,
        WORKER_STACK,
    },
    transport::{Admission, ReceiveEndpoint, SendEndpoint, Transport},
};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, AtomicU8, Ordering},
        Arc, OnceLock,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const ADMITTED: u8 = 1;
const REJECTED_FULL: u8 = 2;
const RECEIVE_WAIT: Duration = Duration::from_millis(10);
const TERMINAL_WAIT_MS: u32 = 30_000;
pub const NORMAL_PERIOD_NS: u64 = 1_000_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Unpaced,
    Normal,
    Saturation,
}

#[derive(Default)]
struct Attempt {
    start: AtomicU32,
    end: AtomicU32,
    receive: AtomicU32,
    admission: AtomicU8,
    seen: AtomicBool,
    planned_release_offset_ns: AtomicU32,
    release_observed_offset_ns: AtomicU32,
}

struct Worker {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    done: AtomicBool,
    admitted: AtomicU32,
}
impl Worker {
    fn new() -> Self {
        Self {
            tid: AtomicI32::new(-1),
            policy: AtomicI32::new(-1),
            priority: AtomicI32::new(-1),
            done: AtomicBool::new(false),
            admitted: AtomicU32::new(0),
        }
    }

    fn configure(&self, priority: i32) -> Result<()> {
        let schedule = support::configure(priority)?;
        self.policy.store(schedule.policy, Ordering::Relaxed);
        self.priority.store(schedule.priority, Ordering::Relaxed);
        self.tid.store(native::tid(), Ordering::Release);
        Ok(())
    }

    fn identity(&self) -> Result<(i32, Schedule)> {
        let tid = self.tid.load(Ordering::Acquire);
        if tid < 1 {
            return Err("fanin-worker-scheduling");
        }
        Ok((
            tid,
            Schedule {
                policy: self.policy.load(Ordering::Relaxed),
                priority: self.priority.load(Ordering::Relaxed),
            },
        ))
    }
}

struct Shared {
    attempts: Vec<Attempt>,
    providers: Vec<Worker>,
    owner: Worker,
    cancelled: AtomicBool,
    failed: AtomicBool,
    release_epoch_ns: OnceLock<u64>,
}
impl Shared {
    fn new(config: Config, providers: usize) -> Self {
        Self {
            attempts: (0..config.total() as usize * providers)
                .map(|_| Attempt::default())
                .collect(),
            providers: (0..providers).map(|_| Worker::new()).collect(),
            owner: Worker::new(),
            cancelled: AtomicBool::new(false),
            failed: AtomicBool::new(false),
            release_epoch_ns: OnceLock::new(),
        }
    }

    fn providers_done(&self) -> bool {
        self.providers
            .iter()
            .all(|worker| worker.done.load(Ordering::Acquire))
    }
}

#[derive(Default)]
struct Handled {
    count: u32,
    checksum: u64,
    retained_checksum: u64,
}

#[derive(Clone, Copy)]
enum Start {
    Concurrent,
    /// Deterministic saturation negative control; never an executable case.
    #[cfg(test)]
    AfterProviders,
    #[cfg(test)]
    FailAfterOneProvider,
}

fn finish_active<T>(
    outcome: Result<T>,
    worker: &Worker,
    shared: &Shared,
    terminal: &Gate,
) -> Result<T> {
    if outcome.is_err() {
        shared.failed.store(true, Ordering::Release);
        shared.cancelled.store(true, Ordering::Release);
    }
    worker.done.store(true, Ordering::Release);
    // Endpoints stay owned until *all* attempts and receives are outside their
    // measurement interval. Early-finished producers may not tear down here.
    terminal.wait(TERMINAL_WAIT_MS)?;
    outcome
}

pub fn run<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    providers: usize,
) -> Result<Report> {
    run_with_mode::<T, P>(config, clock, capacity, providers, Mode::Unpaced)
}

pub fn run_with_mode<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    providers: usize,
    mode: Mode,
) -> Result<Report> {
    run_with_start::<T, P>(config, clock, capacity, providers, mode, Start::Concurrent)
}

fn run_with_start<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    providers: usize,
    mode: Mode,
    start: Start,
) -> Result<Report> {
    if !matches!(providers, 1 | 4)
        || config.samples == 0
        || config.samples > 256
        || config.warmup > 64
        || capacity == 0
    {
        return Err("fanin-bounded-case-parameters");
    }
    if mode == Mode::Saturation && config.total() as usize * providers <= capacity {
        return Err("fanin-saturation-requires-excess-attempts");
    }
    let previous = native::schedule(native::tid())?;
    support::configure(OBSERVER_PRIORITY)?;
    let outcome = execute::<T, P>(config, clock, capacity, providers, mode, start);
    if native::is_nuttx() {
        native::set_schedule(native::tid(), previous)?;
    }
    outcome
}

fn execute<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    providers: usize,
    mode: Mode,
    start: Start,
) -> Result<Report> {
    let shared = Arc::new(Shared::new(config, providers));
    let ready = Gate::new()?;
    let owner_start = Gate::new()?;
    let provider_start = Gate::new()?;
    let terminal = Gate::new()?;
    let (sender, mut receiver) = T::channel::<P>(capacity);
    let mut report = Report::new(config);
    report
        .samples
        .reserve_exact(config.samples as usize * (providers - 1));
    report.provider_schedules.reserve_exact(providers);
    let mut handles: Vec<JoinHandle<Result<()>>> = Vec::with_capacity(providers);
    let (worker, worker_ready, worker_start, worker_terminal) = (
        shared.clone(),
        ready.clone(),
        owner_start.clone(),
        terminal.clone(),
    );
    let owner = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let outcome = (|| {
                let configured = worker.owner.configure(RECEIVER_PRIORITY);
                worker_ready.post()?;
                configured?;
                worker_start.wait(WAIT_MS)?;
                consume::<P, _>(&mut receiver, &worker, config)
            })();
            let outcome = finish_active(outcome, &worker.owner, &worker, &worker_terminal);
            drop(receiver);
            outcome
        })
        .map_err(|_| "fanin-owner-spawn")?;

    let measured = (|| {
        for provider in 0..providers {
            #[cfg(test)]
            if provider == 1 && matches!(start, Start::FailAfterOneProvider) {
                return Err("fanin-injected-provider-spawn-failure");
            }
            let (worker, worker_ready, worker_start, worker_terminal, sender) = (
                shared.clone(),
                ready.clone(),
                provider_start.clone(),
                terminal.clone(),
                sender.clone(),
            );
            let handle = thread::Builder::new()
                .stack_size(WORKER_STACK)
                .spawn(move || {
                    let identity = &worker.providers[provider];
                    let outcome = (|| {
                        let configured = identity.configure(SENDER_PRIORITY);
                        worker_ready.post()?;
                        configured?;
                        worker_start.wait(WAIT_MS)?;
                        for sequence in 1..=config.total() {
                            if worker.cancelled.load(Ordering::Acquire) {
                                return Err("fanin-cancelled");
                            }
                            let attempt = &worker.attempts
                                [provider * config.total() as usize + sequence as usize - 1];
                            let payload = P::new(sequence, provider as u32);
                            let release = if mode == Mode::Unpaced {
                                None
                            } else {
                                Some(release_attempt(&worker, mode, sequence)?)
                            };
                            let before = native::counter();
                            let outcome = sender.try_send(payload);
                            let after = native::counter();
                            attempt.start.store(before, Ordering::Relaxed);
                            attempt.end.store(after, Ordering::Relaxed);
                            if let Some((planned, observed)) = release {
                                attempt
                                    .planned_release_offset_ns
                                    .store(planned, Ordering::Relaxed);
                                attempt
                                    .release_observed_offset_ns
                                    .store(observed, Ordering::Relaxed);
                            }
                            let admitted = match outcome {
                                Ok(()) => {
                                    identity.admitted.fetch_add(1, Ordering::Relaxed);
                                    ADMITTED
                                }
                                Err(Admission::Full(returned)) if returned == payload => {
                                    REJECTED_FULL
                                }
                                _ => return Err("fanin-admission-closed-or-corrupt-rejection"),
                            };
                            // The owner may already have consumed this value.
                            // It never waits for this post-return publication.
                            attempt.admission.store(admitted, Ordering::Release);
                        }
                        Ok(())
                    })();
                    let outcome = finish_active(outcome, identity, &worker, &worker_terminal);
                    drop(sender);
                    outcome
                })
                .map_err(|_| "fanin-provider-spawn")?;
            handles.push(handle);
        }
        for _ in 0..=providers {
            ready.wait(WAIT_MS)?;
        }
        (report.receiver_tid, report.receiver) = shared.owner.identity()?;
        for provider in &shared.providers {
            let identity = provider.identity()?;
            if identity.0 == report.receiver_tid
                || report
                    .provider_schedules
                    .iter()
                    .any(|&(tid, _)| tid == identity.0)
            {
                return Err("fanin-worker-identities-not-distinct");
            }
            report.provider_schedules.push(identity);
        }
        (report.sender_tid, report.sender) = report.provider_schedules[0];
        if mode == Mode::Saturation {
            report.owner_hold_verified =
                support::blocked(report.receiver_tid, report.receiver, || {
                    !shared.owner.done.load(Ordering::Acquire)
                })?;
        }
        let begin_ns = native::monotonic_ns()?;
        if mode != Mode::Unpaced {
            shared
                .release_epoch_ns
                .set(begin_ns)
                .map_err(|_| "fanin-release-epoch-already-set")?;
            report.release_epoch_ns = Some(begin_ns);
        }
        if mode != Mode::Saturation && matches!(start, Start::Concurrent) {
            owner_start.post()?;
        }
        for _ in 0..providers {
            provider_start.post()?;
        }
        if mode == Mode::Saturation {
            support::wait_until(|| {
                shared.providers_done() || shared.failed.load(Ordering::Acquire)
            })?;
            if shared.failed.load(Ordering::Acquire) {
                return Err("fanin-worker-failed");
            }
            report.owner_hold_verified &=
                support::blocked(report.receiver_tid, report.receiver, || {
                    !shared.owner.done.load(Ordering::Acquire) && shared.providers_done()
                })?;
            report.owner_release_ticks = Some(native::counter());
            owner_start.post()?;
        }
        #[cfg(test)]
        if mode != Mode::Saturation && matches!(start, Start::AfterProviders) {
            support::wait_until(|| {
                shared.providers_done() || shared.failed.load(Ordering::Acquire)
            })?;
            owner_start.post()?;
        }
        support::wait_until(|| {
            (shared.owner.done.load(Ordering::Acquire) && shared.providers_done())
                || shared.failed.load(Ordering::Acquire)
        })?;
        if shared.failed.load(Ordering::Acquire) {
            return Err("fanin-worker-failed");
        }
        let end_ns = native::monotonic_ns()?;
        let mut terminal_proof = support::blocked(report.receiver_tid, report.receiver, || {
            shared.owner.done.load(Ordering::Acquire)
        })?;
        for (provider, &(tid, schedule)) in report.provider_schedules.iter().enumerate() {
            terminal_proof &= support::blocked(tid, schedule, || {
                shared.providers[provider].done.load(Ordering::Acquire)
            })?;
        }
        report.terminal_quiescence_verified = terminal_proof;
        Ok((begin_ns, end_ns))
    })();

    // No fallible operation after the first spawn bypasses this rollback. Start
    // gates are released even for partial spawn/setup failures, and cancellation
    // is independent of queue capacity. The owner rechecks it within 10 ms.
    shared.cancelled.store(true, Ordering::Release);
    let mut release_error = owner_start.post().err();
    for _ in 0..handles.len() {
        if let Err(error) = provider_start.post() {
            release_error.get_or_insert(error);
        }
    }
    for _ in 0..=handles.len() {
        if let Err(error) = terminal.post() {
            release_error.get_or_insert(error);
        }
    }
    drop(sender);
    let mut provider_error = None;
    for handle in handles {
        if let Err(error) = support::join_bounded(handle) {
            provider_error.get_or_insert(error);
        }
    }
    let handled = support::join_bounded(owner);
    let (begin_ns, end_ns) = measured?;
    if let Some(error) = release_error.or(provider_error) {
        return Err(error);
    }
    reconcile::<P>(
        &shared,
        config,
        clock,
        begin_ns,
        end_ns,
        handled?,
        &mut report,
    )?;
    if mode == Mode::Saturation
        && (report.counts.admitted as usize != capacity
            || report.counts.rejected as usize != config.total() as usize * providers - capacity)
    {
        return Err("fanin-saturation-counts");
    }
    Ok(report)
}

/// All providers use the same epoch, never a chain of relative sleeps. An
/// overdue release still produces its original attempt and records lateness.
/// Coarse native timers may therefore coalesce slots; that is retained evidence.
fn release_attempt(shared: &Shared, mode: Mode, sequence: u32) -> Result<(u32, u32)> {
    let epoch = *shared
        .release_epoch_ns
        .get()
        .ok_or("fanin-release-epoch-missing")?;
    let planned_offset = match mode {
        Mode::Normal => u64::from(sequence - 1)
            .checked_mul(NORMAL_PERIOD_NS)
            .ok_or("fanin-release-overflow")?,
        Mode::Saturation => 0,
        Mode::Unpaced => return Err("fanin-unpaced-release-marker"),
    };
    let planned = epoch
        .checked_add(planned_offset)
        .ok_or("fanin-release-overflow")?;
    let deadline = Instant::now() + Duration::from_millis(WAIT_MS.into());
    let observed = loop {
        if shared.cancelled.load(Ordering::Acquire) {
            return Err("fanin-cancelled");
        }
        let now = native::monotonic_ns()?;
        if now >= planned {
            break now;
        }
        if Instant::now() >= deadline {
            return Err("fanin-release-deadline");
        }
        thread::sleep(Duration::from_nanos((planned - now).min(NORMAL_PERIOD_NS)));
    };
    let observed_offset = observed
        .checked_sub(epoch)
        .ok_or("fanin-release-clock-reversed")?;
    Ok((
        u32::try_from(planned_offset).map_err(|_| "fanin-release-offset-overflow")?,
        u32::try_from(observed_offset).map_err(|_| "fanin-release-offset-overflow")?,
    ))
}

fn consume<P: Payload, R: ReceiveEndpoint<P>>(
    receiver: &mut R,
    shared: &Shared,
    config: Config,
) -> Result<Handled> {
    let mut last_sequence = [0_u32; 4];
    let mut handled = Handled::default();
    loop {
        if shared.cancelled.load(Ordering::Acquire) {
            return Err("fanin-cancelled");
        }
        if shared.providers_done() {
            let admitted: u32 = shared
                .providers
                .iter()
                .map(|worker| worker.admitted.load(Ordering::Relaxed))
                .sum();
            if handled.count == admitted {
                if receiver.try_recv()?.is_some() {
                    return Err("fanin-extra-message");
                }
                return Ok(handled);
            }
            if handled.count > admitted {
                return Err("fanin-excess-delivery");
            }
        }
        if let Some(payload) = receiver.recv_timeout(RECEIVE_WAIT)? {
            let stamp = native::counter();
            let (provider, sequence) = (payload.provider() as usize, payload.sequence());
            if provider >= shared.providers.len()
                || sequence == 0
                || sequence > config.total()
                || !payload.valid(sequence, provider as u32)
            {
                return Err("fanin-payload-or-identity");
            }
            if sequence <= last_sequence[provider] {
                return Err("fanin-duplicate-or-reordered");
            }
            last_sequence[provider] = sequence;
            let attempt =
                &shared.attempts[provider * config.total() as usize + sequence as usize - 1];
            if attempt.seen.swap(true, Ordering::Relaxed) {
                return Err("fanin-duplicate-delivery");
            }
            attempt.receive.store(stamp, Ordering::Relaxed);
            handled.count += 1;
            handled.checksum = handled.checksum.wrapping_add(payload.checksum());
            if sequence > config.warmup {
                handled.retained_checksum =
                    handled.retained_checksum.wrapping_add(payload.checksum());
            }
        }
    }
}

fn reconcile<P: Payload>(
    shared: &Shared,
    config: Config,
    clock: &Clock,
    begin_ns: u64,
    end_ns: u64,
    handled: Handled,
    report: &mut Report,
) -> Result<()> {
    let mut expected_checksum = 0_u64;
    let mut expected_retained_checksum = 0_u64;
    for provider in 0..shared.providers.len() {
        let mut admitted_count = 0;
        for sequence in 1..=config.total() {
            let attempt =
                &shared.attempts[provider * config.total() as usize + sequence as usize - 1];
            let accepted = match attempt.admission.load(Ordering::Acquire) {
                ADMITTED => true,
                REJECTED_FULL => false,
                _ => return Err("fanin-unaccounted-attempt"),
            };
            if attempt.seen.load(Ordering::Relaxed) != accepted {
                return Err("fanin-admission-delivery-mismatch");
            }
            let start = attempt.start.load(Ordering::Relaxed);
            let admission_end = attempt.end.load(Ordering::Relaxed);
            let receive = if accepted {
                attempt.receive.load(Ordering::Relaxed)
            } else {
                0
            };
            let deltas = [
                admission_end.wrapping_sub(start),
                if accepted {
                    receive.wrapping_sub(start)
                } else {
                    0
                },
            ];
            let window_ns = clock.window(begin_ns, end_ns, &deltas)?;
            let (planned_release_ns, release_observed_ns) =
                if let Some(epoch) = report.release_epoch_ns {
                    let planned = epoch
                        .checked_add(u64::from(
                            attempt.planned_release_offset_ns.load(Ordering::Relaxed),
                        ))
                        .ok_or("fanin-release-overflow")?;
                    let observed = epoch
                        .checked_add(u64::from(
                            attempt.release_observed_offset_ns.load(Ordering::Relaxed),
                        ))
                        .ok_or("fanin-release-overflow")?;
                    if observed < planned || observed > end_ns {
                        return Err("fanin-release-outside-active-window");
                    }
                    (Some(planned), Some(observed))
                } else {
                    (None, None)
                };
            report.counts.attempted += 1;
            if accepted {
                report.counts.admitted += 1;
                admitted_count += 1;
                expected_checksum =
                    expected_checksum.wrapping_add(P::new(sequence, provider as u32).checksum());
                if sequence > config.warmup {
                    expected_retained_checksum = expected_retained_checksum
                        .wrapping_add(P::new(sequence, provider as u32).checksum());
                }
            } else {
                report.counts.rejected += 1;
            }
            if sequence > config.warmup {
                report.samples.push(Sample {
                    sequence,
                    provider: provider as u32,
                    start,
                    admission_end,
                    receive,
                    window_ns,
                    accepted,
                    planned_release_ns,
                    release_observed_ns,
                    ..Sample::default()
                });
            }
        }
        if shared.providers[provider].admitted.load(Ordering::Relaxed) != admitted_count {
            return Err("fanin-provider-admission-count");
        }
    }
    if report.counts.admitted != handled.count
        || handled.checksum != expected_checksum
        || handled.retained_checksum != expected_retained_checksum
    {
        return Err("fanin-handled-count-or-checksum");
    }
    report.counts.handled = handled.count;
    report.checksum = handled.checksum;
    report.retained_checksum = handled.retained_checksum;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        payload::Bytes16,
        transport::{CrossbeamDirect, CrossbeamSelectThree, Messaging, RawStd},
    };

    fn conservation<T: Transport, P: Payload>() {
        let config = Config::new(8, 2).unwrap();
        let clock = Clock::new().unwrap();
        for providers in [1, 4] {
            let report = run::<T, P>(config, &clock, 8, providers).unwrap();
            assert_eq!(report.samples.len(), 8 * providers);
            assert_eq!(report.provider_schedules.len(), providers);
            assert_eq!(report.counts.attempted, 10 * providers as u32);
            assert_eq!(
                report.counts.attempted,
                report.counts.admitted + report.counts.rejected
            );
            assert_eq!(report.counts.admitted, report.counts.handled);
            assert!(report.counts.handled > 0);
            assert!(report.release_epoch_ns.is_none());
            assert!(report.owner_release_ticks.is_none());
            assert!(!report.owner_hold_verified);
            assert!(report.samples.iter().all(|sample| {
                sample.planned_release_ns.is_none() && sample.release_observed_ns.is_none()
            }));
            for provider in 0..providers as u32 {
                let samples: Vec<_> = report
                    .samples
                    .iter()
                    .filter(|sample| sample.provider == provider)
                    .collect();
                assert_eq!(samples.len(), 8);
                assert_eq!(samples.first().unwrap().sequence, 3);
                assert_eq!(samples.last().unwrap().sequence, 10);
                assert!(samples
                    .iter()
                    .all(|sample| sample.accepted || sample.receive == 0));
            }
        }
    }

    #[test]
    fn finite_shared_inbox_reconciles_every_provider_and_attempt() {
        conservation::<RawStd, Bytes16>();
        conservation::<Messaging, imu::ImuSample>();
        conservation::<CrossbeamDirect, Bytes16>();
        conservation::<CrossbeamSelectThree, Bytes16>();
    }

    #[test]
    fn held_owner_saturation_retains_rejected_attempts_and_drains_before_join() {
        let config = Config::new(4, 1).unwrap();
        let report = run_with_start::<Messaging, Bytes16>(
            config,
            &Clock::new().unwrap(),
            1,
            4,
            Mode::Unpaced,
            Start::AfterProviders,
        )
        .unwrap();
        assert_eq!(report.counts.attempted, 20);
        assert_eq!(report.counts.admitted, 1);
        assert_eq!(report.counts.rejected, 19);
        assert_eq!(report.counts.handled, 1);
        assert_eq!(report.samples.len(), 16);
        assert!(report
            .samples
            .iter()
            .all(|sample| !sample.accepted && sample.receive == 0));
    }

    #[test]
    fn finite_case_rejects_unbounded_counts_or_unsupported_provider_topology() {
        let clock = Clock::new().unwrap();
        assert!(run::<RawStd, Bytes16>(Config::new(257, 0).unwrap(), &clock, 8, 1).is_err());
        assert!(run::<RawStd, Bytes16>(Config::new(1, 65).unwrap(), &clock, 8, 1).is_err());
        assert!(run::<RawStd, Bytes16>(Config::new(1, 0).unwrap(), &clock, 8, 2).is_err());
        assert!(run::<RawStd, Bytes16>(Config::new(1, 0).unwrap(), &clock, 0, 1).is_err());
    }

    #[test]
    fn partial_provider_start_rolls_back_the_owner_and_existing_provider() {
        for mode in [Mode::Unpaced, Mode::Normal, Mode::Saturation] {
            let outcome = run_with_start::<RawStd, Bytes16>(
                Config::new(8, 2).unwrap(),
                &Clock::new().unwrap(),
                1,
                4,
                mode,
                Start::FailAfterOneProvider,
            );
            assert!(matches!(
                outcome,
                Err("fanin-injected-provider-spawn-failure")
            ));
        }
    }

    struct FailingOwner;
    struct FailedReceiver<P>(std::sync::mpsc::Receiver<P>);

    impl Transport for FailingOwner {
        const NAME: &'static str = "test-owner-failure";
        const ARMS: usize = 1;
        type Tx<P: Payload> = std::sync::mpsc::SyncSender<P>;
        type Rx<P: Payload> = FailedReceiver<P>;

        fn channel<P: Payload>(capacity: usize) -> (Self::Tx<P>, Self::Rx<P>) {
            let (sender, receiver) = RawStd::channel(capacity);
            (sender, FailedReceiver(receiver))
        }
    }

    impl<P: Payload> ReceiveEndpoint<P> for FailedReceiver<P> {
        fn recv(&mut self) -> Result<P> {
            Err("injected-owner-failure")
        }

        fn try_recv(&mut self) -> Result<Option<P>> {
            ReceiveEndpoint::try_recv(&mut self.0)
        }

        fn recv_timeout(&mut self, _timeout: Duration) -> Result<Option<P>> {
            Err("injected-owner-failure")
        }
    }

    #[test]
    fn owner_failure_cancels_providers_and_joins_despite_full_data_capacity() {
        for mode in [Mode::Unpaced, Mode::Normal, Mode::Saturation] {
            let outcome = run_with_mode::<FailingOwner, Bytes16>(
                Config::new(64, 2).unwrap(),
                &Clock::new().unwrap(),
                1,
                4,
                mode,
            );
            assert!(matches!(outcome, Err("fanin-worker-failed")));
        }
    }

    #[test]
    fn normal_schedule_preserves_absolute_slots_and_every_attempt() {
        let config = Config::new(8, 2).unwrap();
        for providers in [1, 4] {
            let report = run_with_mode::<Messaging, Bytes16>(
                config,
                &Clock::new().unwrap(),
                8,
                providers,
                Mode::Normal,
            )
            .unwrap();
            let epoch = report.release_epoch_ns.unwrap();
            assert_eq!(report.counts.attempted, config.total() * providers as u32);
            assert_eq!(report.samples.len(), config.samples as usize * providers);
            assert_eq!(report.counts.admitted, report.counts.handled);
            assert!(report.owner_release_ticks.is_none());
            let mut last_observed = [epoch; 4];
            for sample in &report.samples {
                let planned = sample.planned_release_ns.unwrap();
                let observed = sample.release_observed_ns.unwrap();
                assert_eq!(
                    planned,
                    epoch + u64::from(sample.sequence - 1) * NORMAL_PERIOD_NS
                );
                assert!(observed >= planned);
                assert!(observed >= last_observed[sample.provider as usize]);
                last_observed[sample.provider as usize] = observed;
            }
        }
    }

    #[test]
    fn executable_saturation_guarantees_full_rejection_and_records_owner_release() {
        let config = Config::new(12, 2).unwrap();
        for providers in [1, 4] {
            let report = run_with_mode::<Messaging, Bytes16>(
                config,
                &Clock::new().unwrap(),
                8,
                providers,
                Mode::Saturation,
            )
            .unwrap();
            assert_eq!(report.counts.attempted, config.total() * providers as u32);
            assert_eq!(report.counts.admitted, 8);
            assert_eq!(report.counts.rejected, report.counts.attempted - 8);
            assert_eq!(report.counts.handled, 8);
            assert_eq!(report.samples.len(), config.samples as usize * providers);
            assert_eq!(report.owner_hold_verified, native::is_nuttx());
            let release = report.owner_release_ticks.unwrap();
            let epoch = report.release_epoch_ns.unwrap();
            let mut full_seen = [false; 4];
            for sample in &report.samples {
                assert_eq!(sample.planned_release_ns, Some(epoch));
                assert!(sample.release_observed_ns.unwrap() >= epoch);
                assert!(release.wrapping_sub(sample.admission_end) < 1 << 31);
                if sample.accepted {
                    assert!(!full_seen[sample.provider as usize]);
                    assert!(sample.receive.wrapping_sub(release) < 1 << 31);
                } else {
                    assert_eq!(sample.receive, 0);
                    full_seen[sample.provider as usize] = true;
                }
            }
        }
        assert!(matches!(
            run_with_mode::<Messaging, Bytes16>(
                Config::new(8, 0).unwrap(),
                &Clock::new().unwrap(),
                8,
                1,
                Mode::Saturation,
            ),
            Err("fanin-saturation-requires-excess-attempts")
        ));
    }

    #[test]
    fn overdue_release_is_observed_and_cancellation_never_waits_for_its_slot() {
        let _clock = Clock::new().unwrap();
        let shared = Shared::new(Config::new(1, 0).unwrap(), 1);
        let now = native::monotonic_ns().unwrap();
        let epoch = now.saturating_sub(2 * NORMAL_PERIOD_NS);
        shared.release_epoch_ns.set(epoch).unwrap();
        let (planned, observed) = release_attempt(&shared, Mode::Normal, 1).unwrap();
        assert_eq!(planned, 0);
        assert!(u64::from(observed) >= now - epoch);
        shared.cancelled.store(true, Ordering::Release);
        assert_eq!(
            release_attempt(&shared, Mode::Normal, 100),
            Err("fanin-cancelled")
        );
    }

    #[test]
    fn conservation_refuses_missing_or_unadmitted_delivery() {
        let config = Config::new(1, 0).unwrap();
        let shared = Shared::new(config, 1);
        let clock = Clock {
            hz: 1_000_000_000,
            source: 7,
            bits: 64,
            resolution_ns: 1,
        };
        let attempt = &shared.attempts[0];
        attempt.admission.store(ADMITTED, Ordering::Relaxed);
        let missing = reconcile::<Bytes16>(
            &shared,
            config,
            &clock,
            0,
            10,
            Handled::default(),
            &mut Report::new(config),
        );
        assert_eq!(missing, Err("fanin-admission-delivery-mismatch"));
        attempt.admission.store(REJECTED_FULL, Ordering::Relaxed);
        attempt.seen.store(true, Ordering::Relaxed);
        let unadmitted = reconcile::<Bytes16>(
            &shared,
            config,
            &clock,
            0,
            10,
            Handled::default(),
            &mut Report::new(config),
        );
        assert_eq!(unadmitted, Err("fanin-admission-delivery-mismatch"));
    }
}

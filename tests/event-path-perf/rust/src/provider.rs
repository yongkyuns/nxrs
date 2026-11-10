//! Native timeout -> task-context synthetic payload -> typed admission -> owner.
//!
//! The absolute CLOCK_MONOTONIC deadline is a planned wake, not an observed IRQ
//! marker. No hardware data is acquired. NuttX's timed-semaphore expiry wakes
//! the provider; only Rust task context constructs or admits a typed payload.
#![deny(unsafe_code)]

use crate::{
    native::{self, Gate, Result, Schedule, State},
    payload::Payload,
    report::{Report, Sample, TimerSample},
    support::{
        self, Clock, Config, OBSERVER_PRIORITY, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS,
        WORKER_STACK,
    },
    transport::{Messaging, ReceiveEndpoint, SendEndpoint, Transport},
};
use std::{
    hint::black_box,
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        Arc, OnceLock,
    },
    thread,
    time::Duration,
};

pub const DEADLINE_DELAY_NS: u64 = 50_000_000;
pub const MAX_SAMPLES: u32 = 128;
pub const MAX_WARMUP: u32 = 32;

// Only this target-header ABI boundary permits unsafe code. The existing Gate
// owns initialized native storage and is kept alive until both workers join.
mod timer {
    #![allow(unsafe_code)]
    use crate::native::{Gate, Result};
    use std::ffi::c_void;

    unsafe extern "C" {
        fn event_path_timer_wait_abs(
            storage: *mut c_void,
            absolute_ns: u64,
            expired: *mut i32,
        ) -> i32;
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Wake {
        Expired,
        Posted,
    }

    pub fn wait(gate: &Gate, deadline_ns: u64) -> Result<Wake> {
        let mut expired = -1;
        if unsafe { event_path_timer_wait_abs(gate.pointer(), deadline_ns, &mut expired) } != 0 {
            return Err("timer-native-wait-error");
        }
        match expired {
            1 => Ok(Wake::Expired),
            0 => Ok(Wake::Posted),
            _ => Err("timer-native-wait-outcome"),
        }
    }
}

struct Worker {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    announced: AtomicU32,
    resumed: AtomicU32,
    completed: AtomicU32,
    failed: AtomicBool,
    terminal: AtomicBool,
    ready: Arc<Gate>,
    arm: Arc<Gate>,
    done: Arc<Gate>,
    finish: Arc<Gate>,
}

impl Worker {
    fn new() -> Result<Arc<Self>> {
        Ok(Arc::new(Self {
            tid: AtomicI32::new(-1),
            policy: AtomicI32::new(-1),
            priority: AtomicI32::new(-1),
            announced: AtomicU32::new(0),
            resumed: AtomicU32::new(0),
            completed: AtomicU32::new(0),
            failed: AtomicBool::new(false),
            terminal: AtomicBool::new(false),
            ready: Gate::new()?,
            arm: Gate::new()?,
            done: Gate::new()?,
            finish: Gate::new()?,
        }))
    }

    fn configure(&self, priority: i32) -> Result<()> {
        let actual = support::configure(priority);
        if let Ok(schedule) = actual {
            self.policy.store(schedule.policy, Ordering::Relaxed);
            self.priority.store(schedule.priority, Ordering::Relaxed);
            self.tid.store(native::tid(), Ordering::Release);
        }
        self.ready.post()?;
        actual.map(|_| ())
    }

    fn identity(&self) -> Result<(i32, Schedule)> {
        self.ready.wait(WAIT_MS)?;
        let tid = self.tid.load(Ordering::Acquire);
        if tid < 1 || self.failed.load(Ordering::Acquire) {
            return Err("timer-worker-scheduling");
        }
        Ok((
            tid,
            Schedule {
                policy: self.policy.load(Ordering::Relaxed),
                priority: self.priority.load(Ordering::Relaxed),
            },
        ))
    }

    fn announce_observed(&self, sequence: u32) -> Result<()> {
        support::wait_until(|| {
            self.announced.load(Ordering::Acquire) == sequence
                || self.failed.load(Ordering::Acquire)
        })?;
        if self.failed.load(Ordering::Acquire) {
            return Err("timer-worker-failed");
        }
        Ok(())
    }

    fn complete_observed(&self, sequence: u32) -> Result<()> {
        self.done.wait(WAIT_MS)?;
        if self.failed.load(Ordering::Acquire) || self.completed.load(Ordering::Acquire) != sequence
        {
            return Err("timer-worker-failed-or-sequence");
        }
        Ok(())
    }

    fn hold(&self) -> Result<()> {
        self.terminal.store(true, Ordering::Release);
        self.finish.wait(WAIT_MS)
    }

    fn expose_failure(&self, result: Result<()>) -> Result<()> {
        if result.is_err() {
            self.failed.store(true, Ordering::Release);
            self.done.post()?;
        }
        result
    }
}

#[derive(Clone, Copy)]
struct Plan {
    plan_ns: u64,
    deadline_ns: u64,
}

#[derive(Clone, Copy)]
struct ProviderRecord {
    resume_ns: u64,
    resume_ticks: u32,
    admission_start: u32,
    admission_end: u32,
}

#[derive(Clone, Copy)]
struct OwnerRecord {
    receive: u32,
    handler_start: u32,
    handler_end: u32,
    checksum: u64,
}

#[derive(Default)]
struct Slot {
    plan: OnceLock<Plan>,
    provider: OnceLock<ProviderRecord>,
    owner: OnceLock<OwnerRecord>,
}

struct Shared {
    cancelled: AtomicBool,
    timer: Arc<Gate>,
    provider: Arc<Worker>,
    owner: Arc<Worker>,
    slots: Vec<Slot>,
}

#[derive(Clone, Copy, Default)]
struct Hooks {
    #[cfg(test)]
    fail_provider_spawn: bool,
    #[cfg(test)]
    cancel_after_timer_announcement: u32,
    #[cfg(test)]
    post_instead_of_expiry: u32,
    #[cfg(test)]
    corrupt_payload: u32,
}

fn owner_loop<P: Payload>(
    shared: &Shared,
    config: Config,
    mut receiver: <Messaging as Transport>::Rx<P>,
) -> Result<()> {
    let owner = &shared.owner;
    owner.configure(RECEIVER_PRIORITY)?;
    for sequence in 1..=config.total() {
        owner.arm.wait(WAIT_MS)?;
        if shared.cancelled.load(Ordering::Acquire) {
            return Ok(());
        }
        // No intervening wait: a fresh WAIT_SEM observation belongs to this
        // exact Inbox::wait(None) call, before the provider is released.
        owner.announced.store(sequence, Ordering::Release);
        let received = receiver.recv();
        let receive = native::counter();
        if shared.cancelled.load(Ordering::Acquire) {
            return Ok(());
        }
        let received = received?;
        let handler_start = native::counter();
        let received = black_box(received);
        let (valid, checksum) = black_box((received.valid(sequence, 0), received.checksum()));
        let handler_end = native::counter();
        if !valid {
            return Err("timer-owner-payload");
        }
        shared.slots[(sequence - 1) as usize]
            .owner
            .set(OwnerRecord {
                receive,
                handler_start,
                handler_end,
                checksum,
            })
            .map_err(|_| "timer-duplicate-owner-completion")?;
        owner.completed.store(sequence, Ordering::Release);
        owner.done.post()?;
    }
    // In particular, the last high-priority owner cannot destroy its queue
    // or thread resources before the provider has stamped try_send's return.
    owner.hold()
}

fn provider_loop<P: Payload>(
    shared: &Shared,
    config: Config,
    sender: <Messaging as Transport>::Tx<P>,
    _hooks: Hooks,
) -> Result<()> {
    let provider = &shared.provider;
    provider.configure(SENDER_PRIORITY)?;
    for sequence in 1..=config.total() {
        provider.arm.wait(WAIT_MS)?;
        if shared.cancelled.load(Ordering::Acquire) {
            return Ok(());
        }
        let slot = &shared.slots[(sequence - 1) as usize];
        let plan = *slot.plan.get().ok_or("timer-missing-plan")?;
        if native::monotonic_ns()? >= plan.deadline_ns {
            return Err("timer-deadline-passed-before-wait");
        }
        // The dedicated semaphore is never posted during successful work.
        // After this publication the only wait is the native absolute timer.
        provider.announced.store(sequence, Ordering::Release);
        let wake = timer::wait(&shared.timer, plan.deadline_ns);
        let resume_ticks = native::counter();
        provider.resumed.store(sequence, Ordering::Release);
        let resume_ns = native::monotonic_ns()?;
        if shared.cancelled.load(Ordering::Acquire) {
            return Ok(());
        }
        if wake? != timer::Wake::Expired {
            return Err("timer-wake-was-not-expiry");
        }
        if resume_ns < plan.deadline_ns {
            return Err("timer-resumed-before-deadline");
        }

        // This is deliberately after the native wake: synthetic normalization
        // belongs to task context and is visible between resume and admission.
        let value = P::new(sequence, 0);
        #[cfg(test)]
        let value = if _hooks.corrupt_payload == sequence {
            P::new(sequence + 1, 0)
        } else {
            value
        };
        let admission_start = native::counter();
        let admitted = SendEndpoint::try_send(&sender, value);
        let admission_end = native::counter();
        admitted.map_err(|_| "timer-admission-must-succeed")?;
        slot.provider
            .set(ProviderRecord {
                resume_ns,
                resume_ticks,
                admission_start,
                admission_end,
            })
            .map_err(|_| "timer-duplicate-provider-completion")?;
        provider.completed.store(sequence, Ordering::Release);
        provider.done.post()?;
    }
    provider.hold()
}

fn timer_wait_proof(
    shared: &Shared,
    sequence: u32,
    tid: i32,
    schedule: Schedule,
    plan: Plan,
) -> Result<(bool, u64)> {
    if !native::is_nuttx() {
        return Ok((false, 0));
    }
    loop {
        let now = native::monotonic_ns()?;
        if now >= plan.deadline_ns
            || shared.provider.resumed.load(Ordering::Acquire) >= sequence
            || shared.provider.failed.load(Ordering::Acquire)
        {
            return Err("timer-wait-not-proven-before-expiry");
        }
        let (state, priority) = native::state(tid);
        if state == State::WaitSem
            && priority == schedule.priority
            && shared.provider.announced.load(Ordering::Acquire) == sequence
        {
            // Stamp after the TCB read and recheck completion so a transition
            // during observation cannot qualify a stale timed wait.
            let observed = native::monotonic_ns()?;
            if observed < plan.deadline_ns
                && shared.provider.resumed.load(Ordering::Acquire) < sequence
            {
                return Ok((true, observed));
            }
        }
        thread::sleep(Duration::from_millis(1));
    }
}

fn window(
    clock: &Clock,
    plan: Plan,
    provider: ProviderRecord,
    owner: OwnerRecord,
    end_ns: u64,
) -> Result<u64> {
    if provider.resume_ns < plan.deadline_ns {
        return Err("timer-resumed-before-deadline");
    }
    let origin = provider.resume_ticks;
    let start = provider.admission_start.wrapping_sub(origin);
    let admission_end = provider.admission_end.wrapping_sub(origin);
    let receive = owner.receive.wrapping_sub(origin);
    let handler_start = owner.handler_start.wrapping_sub(origin);
    let handler_end = owner.handler_end.wrapping_sub(origin);
    if start > admission_end
        || start > receive
        || receive > handler_start
        || handler_start > handler_end
    {
        return Err("timer-stage-order");
    }
    // The guard starts at this sequence's deadline, not a prior iteration or
    // boot epoch. Admission return may trail the preempting owner handler.
    clock.window(
        plan.deadline_ns,
        end_ns,
        &[start, admission_end, receive, handler_start, handler_end],
    )
}

pub fn run<P: Payload>(config: Config, clock: &Clock, capacity: usize) -> Result<Report> {
    run_with::<P>(config, clock, capacity, Hooks::default())
}

fn run_with<P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    hooks: Hooks,
) -> Result<Report> {
    if config.samples == 0
        || config.samples > MAX_SAMPLES
        || config.warmup > MAX_WARMUP
        || capacity == 0
    {
        return Err("timer-bounded-case-parameters");
    }
    support::configure(OBSERVER_PRIORITY)?;
    let mut report = Report::new(config);
    let shared = Arc::new(Shared {
        cancelled: AtomicBool::new(false),
        timer: Gate::new()?,
        provider: Worker::new()?,
        owner: Worker::new()?,
        slots: (0..config.total()).map(|_| Slot::default()).collect(),
    });
    let (sender, receiver) = Messaging::channel::<P>(capacity);
    let owner = shared.clone();
    let owner_handle = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let result = owner_loop::<P>(&owner, config, receiver);
            owner.owner.expose_failure(result)
        })
        .map_err(|_| "timer-owner-spawn")?;
    let mut provider_handle = None;
    let measured = (|| {
        #[cfg(test)]
        if hooks.fail_provider_spawn {
            return Err("timer-injected-provider-spawn-failure");
        }
        let provider = shared.clone();
        let provider_sender = sender.clone();
        provider_handle = Some(
            thread::Builder::new()
                .stack_size(WORKER_STACK)
                .spawn(move || {
                    let result = provider_loop::<P>(&provider, config, provider_sender, hooks);
                    provider.provider.expose_failure(result)
                })
                .map_err(|_| "timer-provider-spawn")?,
        );
        (report.receiver_tid, report.receiver) = shared.owner.identity()?;
        (report.sender_tid, report.sender) = shared.provider.identity()?;
        if report.sender_tid == report.receiver_tid
            || report.sender_tid == native::tid()
            || report.receiver_tid == native::tid()
        {
            return Err("timer-worker-identities-not-distinct");
        }
        for sequence in 1..=config.total() {
            shared.owner.arm.post()?;
            shared.owner.announce_observed(sequence)?;
            let owner_wait_verified =
                support::blocked(report.receiver_tid, report.receiver, || {
                    shared.owner.announced.load(Ordering::Acquire) == sequence
                        && shared.owner.completed.load(Ordering::Acquire) < sequence
                })?;
            let owner_wait_observed_ns = if owner_wait_verified {
                native::monotonic_ns()?
            } else {
                0
            };
            let plan_ns = native::monotonic_ns()?;
            let plan = Plan {
                plan_ns,
                deadline_ns: plan_ns
                    .checked_add(DEADLINE_DELAY_NS)
                    .ok_or("timer-deadline-overflow")?,
            };
            let slot = &shared.slots[(sequence - 1) as usize];
            slot.plan.set(plan).map_err(|_| "timer-duplicate-plan")?;
            shared.provider.arm.post()?;
            shared.provider.announce_observed(sequence)?;
            let (provider_wait_verified, provider_wait_observed_ns) =
                timer_wait_proof(&shared, sequence, report.sender_tid, report.sender, plan)?;
            #[cfg(test)]
            if hooks.cancel_after_timer_announcement == sequence {
                return Err("timer-injected-cancellation");
            }
            #[cfg(test)]
            if hooks.post_instead_of_expiry == sequence {
                shared.timer.post()?;
            }

            // Wait for the provider first. On FIFO targets the owner can finish
            // inside try_send; its completion must not wake this higher-priority
            // observer before the provider records admission return.
            shared.provider.complete_observed(sequence)?;
            shared.owner.complete_observed(sequence)?;
            let end_ns = native::monotonic_ns()?;
            let provider = *slot.provider.get().ok_or("timer-provider-record-missing")?;
            let owner = *slot.owner.get().ok_or("timer-owner-record-missing")?;
            let window_ns = window(clock, plan, provider, owner, end_ns)?;
            if owner.checksum != P::new(sequence, 0).checksum() {
                return Err("timer-handler-checksum");
            }
            report.counts.attempted += 1;
            report.counts.admitted += 1;
            report.counts.handled += 1;
            report.checksum = report.checksum.wrapping_add(owner.checksum);
            if sequence > config.warmup {
                report.retained_checksum = report.retained_checksum.wrapping_add(owner.checksum);
                report.samples.push(Sample {
                    sequence,
                    start: provider.admission_start,
                    admission_end: provider.admission_end,
                    receive: owner.receive,
                    window_ns,
                    blocked: owner_wait_verified,
                    timer: Some(TimerSample {
                        plan_ns: plan.plan_ns,
                        deadline_ns: plan.deadline_ns,
                        provider_resume_ns: provider.resume_ns,
                        provider_resume_ticks: provider.resume_ticks,
                        provider_wait_observed_ns,
                        owner_wait_observed_ns,
                        provider_wait_verified,
                        expired: true,
                        handler_start_ticks: owner.handler_start,
                        handler_end_ticks: owner.handler_end,
                    }),
                    ..Sample::default()
                });
            }
        }
        support::wait_until(|| {
            shared.owner.terminal.load(Ordering::Acquire)
                && shared.provider.terminal.load(Ordering::Acquire)
        })?;
        let owner_hold = support::blocked(report.receiver_tid, report.receiver, || {
            shared.owner.terminal.load(Ordering::Acquire)
        })?;
        let provider_hold = support::blocked(report.sender_tid, report.sender, || {
            shared.provider.terminal.load(Ordering::Acquire)
        })?;
        report.terminal_quiescence_verified = owner_hold && provider_hold;
        Ok(())
    })();

    // Release every wait site even after partial startup. Cancellation is an
    // out-of-band semaphore post; queue capacity cannot block shutdown. Closing
    // the supervisor sender and joining the provider closes the last endpoint,
    // which also releases an owner still waiting in Inbox::wait(None).
    shared.cancelled.store(true, Ordering::Release);
    let releases = [
        shared.timer.post(),
        shared.provider.arm.post(),
        shared.provider.finish.post(),
        shared.owner.arm.post(),
        shared.owner.finish.post(),
    ];
    drop(sender);
    let provider_joined = match provider_handle {
        Some(handle) => support::join_bounded(handle),
        None => Ok(()),
    };
    let owner_joined = support::join_bounded(owner_handle);
    measured?;
    for release in releases {
        release?;
    }
    provider_joined?;
    owner_joined?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::payload::Bytes16;

    #[test]
    fn native_timeout_and_post_have_distinct_outcomes_and_timeout_restores_count() {
        let gate = Gate::new().unwrap();
        for _ in 0..2 {
            let deadline = native::monotonic_ns().unwrap() + 2_000_000;
            assert_eq!(timer::wait(&gate, deadline), Ok(timer::Wake::Expired));
        }
        gate.post().unwrap();
        assert_eq!(
            timer::wait(&gate, native::monotonic_ns().unwrap() + 1_000_000_000),
            Ok(timer::Wake::Posted)
        );
    }

    #[test]
    fn cancellation_post_wakes_native_wait_without_waiting_for_its_deadline() {
        let gate = Gate::new().unwrap();
        let announced = Arc::new(AtomicBool::new(false));
        let (worker_gate, worker_announced) = (gate.clone(), announced.clone());
        let worker = thread::spawn(move || {
            let deadline = native::monotonic_ns()? + 5_000_000_000;
            worker_announced.store(true, Ordering::Release);
            timer::wait(&worker_gate, deadline)
        });
        support::wait_until(|| announced.load(Ordering::Acquire)).unwrap();
        gate.post().unwrap();
        assert_eq!(support::join_bounded(worker), Ok(timer::Wake::Posted));
    }

    fn checked_path<P: Payload>(capacity: usize) {
        let config = Config::new(2, 1).unwrap();
        let clock = Clock::new().unwrap();
        let report = run::<P>(config, &clock, capacity).unwrap();
        assert_eq!(report.samples.len(), 2);
        assert_eq!(report.counts.attempted, 3);
        assert_eq!(report.counts.admitted, 3);
        assert_eq!(report.counts.rejected, 0);
        assert_eq!(report.counts.handled, 3);
        assert_ne!(report.sender_tid, report.receiver_tid);
        let sum = |range: std::ops::RangeInclusive<u32>| {
            range.fold(0_u64, |sum, sequence| {
                sum.wrapping_add(P::new(sequence, 0).checksum())
            })
        };
        assert_eq!(report.checksum, sum(1..=3));
        assert_eq!(report.retained_checksum, sum(2..=3));
        for sample in report.samples {
            let timer = sample.timer.unwrap();
            assert_eq!(timer.deadline_ns - timer.plan_ns, DEADLINE_DELAY_NS);
            assert!(timer.provider_resume_ns >= timer.deadline_ns);
            assert!(timer.expired);
            if !native::is_nuttx() {
                assert!(!timer.provider_wait_verified);
                assert!(!sample.blocked);
                assert_eq!(timer.provider_wait_observed_ns, 0);
                assert_eq!(timer.owner_wait_observed_ns, 0);
            }
        }
    }

    #[test]
    fn real_messaging_timer_path_checks_baseline_and_imu_handler_work() {
        checked_path::<Bytes16>(8);
        checked_path::<imu::ImuSample>(16);
    }

    #[test]
    fn partial_start_and_early_or_later_timer_cancellation_release_and_join() {
        let clock = Clock::new().unwrap();
        let config = Config::new(2, 0).unwrap();
        assert!(matches!(
            run_with::<Bytes16>(
                config,
                &clock,
                8,
                Hooks {
                    fail_provider_spawn: true,
                    ..Hooks::default()
                }
            ),
            Err("timer-injected-provider-spawn-failure")
        ));
        for sequence in [1, 2] {
            assert!(matches!(
                run_with::<Bytes16>(
                    config,
                    &clock,
                    8,
                    Hooks {
                        cancel_after_timer_announcement: sequence,
                        ..Hooks::default()
                    }
                ),
                Err("timer-injected-cancellation")
            ));
        }
    }

    #[test]
    fn posted_wake_or_corrupt_handled_payload_cannot_be_reported_as_timer_work() {
        let clock = Clock::new().unwrap();
        let config = Config::new(2, 0).unwrap();
        assert!(run_with::<Bytes16>(
            config,
            &clock,
            8,
            Hooks {
                post_instead_of_expiry: 1,
                ..Hooks::default()
            }
        )
        .is_err());
        assert!(run_with::<Bytes16>(
            config,
            &clock,
            8,
            Hooks {
                corrupt_payload: 2,
                ..Hooks::default()
            }
        )
        .is_err());
    }

    #[test]
    fn timer_limits_refuse_runs_that_cannot_fit_the_outer_watchdog_budget() {
        let clock = Clock::new().unwrap();
        for config in [
            Config::new(MAX_SAMPLES + 1, 0).unwrap(),
            Config::new(1, MAX_WARMUP + 1).unwrap(),
        ] {
            assert!(matches!(
                run::<Bytes16>(config, &clock, 8),
                Err("timer-bounded-case-parameters")
            ));
        }
    }

    #[test]
    fn handler_before_receive_and_ambiguous_counter_windows_are_refused() {
        let clock = Clock {
            hz: 1_000_000_000,
            source: 4,
            bits: 64,
            resolution_ns: 1,
        };
        let plan = Plan {
            plan_ns: 0,
            deadline_ns: DEADLINE_DELAY_NS,
        };
        let provider = ProviderRecord {
            resume_ns: DEADLINE_DELAY_NS,
            resume_ticks: 100,
            admission_start: 102,
            admission_end: 110,
        };
        let mut owner = OwnerRecord {
            receive: 105,
            handler_start: 104,
            handler_end: 108,
            checksum: 0,
        };
        assert_eq!(
            window(&clock, plan, provider, owner, DEADLINE_DELAY_NS + 20),
            Err("timer-stage-order")
        );
        owner.handler_start = 106;
        assert!(window(&clock, plan, provider, owner, DEADLINE_DELAY_NS + 20).is_ok());
        assert_eq!(
            window(&clock, plan, provider, owner, DEADLINE_DELAY_NS + (1 << 31)),
            Err("counter-wrap-ambiguous")
        );
    }
}

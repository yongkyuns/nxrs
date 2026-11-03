//! Payload-free scheduling controls for the P2 timing ladder.
//!
//! Notification measurements include the Release publication of a sequence.
//! A semaphore token or an unpark token alone is never counted as useful work.
//! These controls do not have the queue/admission semantics of a transport.
#![forbid(unsafe_code)]

use crate::{
    native::{self, Gate, Result, Schedule, State},
    report::{Report, Sample},
    support::{
        self, Clock, Config, OBSERVER_PRIORITY, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS,
        WORKER_STACK,
    },
};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        Arc,
    },
    thread::{self, Thread},
};

struct NotificationState {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    announced: AtomicU32,
    published: AtomicU32,
    completed: AtomicU32,
    ticks: AtomicU32,
    returns: AtomicU32,
    cancelled: AtomicBool,
    terminal_ready: AtomicBool,
}

impl NotificationState {
    fn new() -> Self {
        Self {
            tid: AtomicI32::new(-1),
            policy: AtomicI32::new(-1),
            priority: AtomicI32::new(-1),
            announced: AtomicU32::new(0),
            published: AtomicU32::new(0),
            completed: AtomicU32::new(0),
            ticks: AtomicU32::new(0),
            returns: AtomicU32::new(0),
            cancelled: AtomicBool::new(false),
            terminal_ready: AtomicBool::new(false),
        }
    }
}

struct Wake {
    ticks: u32,
    returns: u32,
}

trait Notification: Send + Sync + 'static {
    fn wait(&self, state: &NotificationState, expected: u32) -> Result<Wake>;
    fn notify(&self, worker: &Thread) -> Result<()>;
}

struct Semaphore(Arc<Gate>);

impl Notification for Semaphore {
    fn wait(&self, state: &NotificationState, expected: u32) -> Result<Wake> {
        self.0.wait(WAIT_MS)?;
        let ticks = native::counter();
        if state.cancelled.load(Ordering::Acquire) {
            return Err("notification-cancelled");
        }
        if state.published.load(Ordering::Acquire) != expected {
            return Err("semaphore-sequence");
        }
        Ok(Wake { ticks, returns: 0 })
    }

    fn notify(&self, _worker: &Thread) -> Result<()> {
        self.0.post()
    }
}

struct Park;

impl Notification for Park {
    #[inline]
    fn wait(&self, state: &NotificationState, expected: u32) -> Result<Wake> {
        // This monomorphizes to ordinary thread::park. Only unit tests supply
        // deterministic token/race callbacks to the same predicate loop.
        park_predicate(state, expected, thread::park)
    }

    fn notify(&self, worker: &Thread) -> Result<()> {
        worker.unpark();
        Ok(())
    }
}

#[inline]
fn park_predicate(
    state: &NotificationState,
    expected: u32,
    mut do_park: impl FnMut(),
) -> Result<Wake> {
    let mut returns = 0u32;
    loop {
        if state.cancelled.load(Ordering::Acquire) {
            return Err("notification-cancelled");
        }
        let observed = state.published.load(Ordering::Acquire);
        if observed == expected {
            return Ok(Wake {
                ticks: native::counter(),
                returns,
            });
        }
        if observed > expected {
            return Err("park-sequence-skipped");
        }
        do_park();
        returns = returns.checked_add(1).ok_or("park-return-overflow")?;
        // Spurious/stale/coalesced tokens cannot advance completed. A publish
        // between the Acquire predicate read and park leaves a wake token.
    }
}

pub fn semaphore(config: Config, clock: &Clock) -> Result<Report> {
    notification(config, clock, Semaphore(Gate::new()?), |_| Ok(()))
}

pub fn park(config: Config, clock: &Clock) -> Result<Report> {
    notification(config, clock, Park, |_| Ok(()))
}

fn notification<N: Notification>(
    config: Config,
    clock: &Clock,
    notification: N,
    mut before_release: impl FnMut(u32) -> Result<()>,
) -> Result<Report> {
    let mut report = Report::new(config);
    report.sender = support::configure(SENDER_PRIORITY)?;
    report.sender_tid = native::tid();
    let state = Arc::new(NotificationState::new());
    let ready = Gate::new()?;
    let arm = Gate::new()?;
    let done = Gate::new()?;
    let terminal = Gate::new()?;
    let notify = Arc::new(notification);
    let (worker, worker_ready, worker_arm, worker_done, worker_terminal, worker_notify) = (
        state.clone(),
        ready.clone(),
        arm.clone(),
        done.clone(),
        terminal.clone(),
        notify.clone(),
    );
    let handle = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let actual = support::configure(RECEIVER_PRIORITY);
            if let Ok(schedule) = actual {
                worker.policy.store(schedule.policy, Ordering::Relaxed);
                worker.priority.store(schedule.priority, Ordering::Relaxed);
                worker.tid.store(native::tid(), Ordering::Release);
            }
            worker_ready.post()?;
            actual?;
            for sequence in 1..=config.total() {
                worker_arm.wait(WAIT_MS)?;
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                // No other wait, gate, sleep, allocation or log between this
                // fresh announcement and the measured semaphore/park call.
                worker.announced.store(sequence, Ordering::Release);
                let wake = match worker_notify.wait(&worker, sequence) {
                    Err(_) if worker.cancelled.load(Ordering::Acquire) => return Ok(()),
                    value => value?,
                };
                worker.ticks.store(wake.ticks, Ordering::Relaxed);
                worker.returns.store(wake.returns, Ordering::Relaxed);
                worker.completed.store(sequence, Ordering::Release);
                worker_done.post()?;
            }
            worker.terminal_ready.store(true, Ordering::Release);
            worker_terminal.wait(WAIT_MS)?;
            Ok(())
        })
        .map_err(|_| "notification-worker-spawn")?;

    let measurement = (|| {
        ready.wait(WAIT_MS)?;
        let tid = state.tid.load(Ordering::Acquire);
        if tid <= 0 || tid == report.sender_tid {
            return Err("notification-worker-identity");
        }
        report.receiver_tid = tid;
        report.receiver = Schedule {
            policy: state.policy.load(Ordering::Relaxed),
            priority: state.priority.load(Ordering::Relaxed),
        };
        for sequence in 1..=config.total() {
            arm.post()?;
            support::wait_until(|| state.announced.load(Ordering::Acquire) == sequence)?;
            let blocked = support::blocked(tid, report.receiver, || {
                state.announced.load(Ordering::Acquire) == sequence
                    && state.completed.load(Ordering::Acquire) < sequence
            })?;
            // Production instantiations use an inlined no-op. Tests fail here
            // to prove cancellation handles a live wait and a partial run.
            before_release(sequence)?;
            let window_begin = native::monotonic_ns()?;
            let start = native::counter();
            state.published.store(sequence, Ordering::Release);
            notify.notify(handle.thread())?;
            let admission_end = native::counter();
            done.wait(WAIT_MS)?;
            let window_end = native::monotonic_ns()?;
            if state.completed.load(Ordering::Acquire) != sequence {
                return Err("notification-completion-sequence");
            }
            let receive = state.ticks.load(Ordering::Relaxed);
            let returns = state.returns.load(Ordering::Relaxed);
            let window_ns = clock.window(
                window_begin,
                window_end,
                &[
                    admission_end.wrapping_sub(start),
                    receive.wrapping_sub(start),
                ],
            )?;
            report.counts.attempted += 1;
            report.counts.admitted += 1;
            report.counts.handled += 1;
            report.checksum = report.checksum.wrapping_add(u64::from(sequence));
            report.park_returns = report
                .park_returns
                .checked_add(returns)
                .ok_or("park-return-overflow")?;
            if sequence > config.warmup {
                report.retained_checksum =
                    report.retained_checksum.wrapping_add(u64::from(sequence));
                report.samples.push(Sample {
                    sequence,
                    start,
                    admission_end,
                    receive,
                    window_ns,
                    blocked,
                    peer_tid: tid,
                    park_returns: returns,
                    ..Sample::default()
                });
            }
        }
        support::wait_until(|| state.terminal_ready.load(Ordering::Acquire))?;
        report.terminal_quiescence_verified = support::blocked(tid, report.receiver, || {
            state.terminal_ready.load(Ordering::Acquire)
        })?;
        Ok(())
    })();

    // Failure never relies on admission into a measured queue. This wakes the
    // worker whether it is before arm, in the measured wait, or at final hold.
    state.cancelled.store(true, Ordering::Release);
    let armed = arm.post();
    let notified = notify.notify(handle.thread());
    let released = terminal.post();
    let joined = support::join_bounded(handle);
    measurement?;
    armed?;
    notified?;
    released?;
    joined?;
    Ok(report)
}

struct PeerState {
    sender_tid: AtomicI32,
    receiver_tid: AtomicI32,
    sender_policy: AtomicI32,
    receiver_policy: AtomicI32,
    sender_priority: AtomicI32,
    receiver_priority: AtomicI32,
    active: AtomicBool,
    turn: AtomicU32,
    completed: AtomicU32,
    ticks: AtomicU32,
    observed_tid: AtomicI32,
    cancelled: AtomicBool,
    sender_terminal: AtomicBool,
    receiver_terminal: AtomicBool,
}

impl PeerState {
    fn new() -> Self {
        Self {
            sender_tid: AtomicI32::new(-1),
            receiver_tid: AtomicI32::new(-1),
            sender_policy: AtomicI32::new(-1),
            receiver_policy: AtomicI32::new(-1),
            sender_priority: AtomicI32::new(-1),
            receiver_priority: AtomicI32::new(-1),
            active: AtomicBool::new(false),
            turn: AtomicU32::new(0),
            completed: AtomicU32::new(0),
            ticks: AtomicU32::new(0),
            observed_tid: AtomicI32::new(-1),
            cancelled: AtomicBool::new(false),
            sender_terminal: AtomicBool::new(false),
            receiver_terminal: AtomicBool::new(false),
        }
    }
}

fn verify_peer_marker(state: &PeerState, sequence: u32, expected_tid: i32) -> Result<()> {
    if state.completed.load(Ordering::Acquire) != sequence {
        return Err("yield-did-not-run-peer");
    }
    if expected_tid <= 0 || state.observed_tid.load(Ordering::Relaxed) != expected_tid {
        return Err("yield-peer-identity");
    }
    Ok(())
}

/// Yield to a known runnable peer. The two application markers do not count
/// intervening scheduler switches and do not enclose pure context-save cost.
pub fn fixed_peer(config: Config, clock: &Clock) -> Result<Report> {
    // native.c refuses SMP/non-flat NuttX at compile time. Linux execution does
    // not establish this scheduling contract, even when a yield happens to run.
    if !native::is_nuttx() {
        return Err("fixed-peer-requires-single-core-fifo");
    }
    support::configure(OBSERVER_PRIORITY)?;
    let state = Arc::new(PeerState::new());
    let ready = Gate::new()?;
    let arm_sender = Gate::new()?;
    let arm_receiver = Gate::new()?;
    let finished = Gate::new()?;
    let terminal_sender = Gate::new()?;
    let terminal_receiver = Gate::new()?;
    let (peer, peer_ready, peer_arm, peer_terminal) = (
        state.clone(),
        ready.clone(),
        arm_receiver.clone(),
        terminal_receiver.clone(),
    );
    let receiver = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let configured = support::configure(SENDER_PRIORITY);
            if let Ok(actual) = configured {
                peer.receiver_policy.store(actual.policy, Ordering::Relaxed);
                peer.receiver_priority
                    .store(actual.priority, Ordering::Relaxed);
                peer.receiver_tid.store(native::tid(), Ordering::Release);
            }
            peer_ready.post()?;
            configured?;
            peer_arm.wait(WAIT_MS)?;
            if peer.cancelled.load(Ordering::Acquire) {
                return Ok(());
            }
            peer.active.store(true, Ordering::Release);
            for sequence in 1..=config.total() {
                loop {
                    if peer.cancelled.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    let turn = peer.turn.load(Ordering::Acquire);
                    if turn == sequence {
                        break;
                    }
                    if turn > sequence {
                        return Err("peer-sequence-skipped");
                    }
                    // The peer remains runnable. This is neither a blocking
                    // notification nor a claimed scheduler switch count.
                    thread::yield_now();
                }
                let ticks = native::counter();
                peer.ticks.store(ticks, Ordering::Relaxed);
                peer.observed_tid.store(native::tid(), Ordering::Relaxed);
                peer.completed.store(sequence, Ordering::Release);
                thread::yield_now();
            }
            peer.receiver_terminal.store(true, Ordering::Release);
            peer_terminal.wait(WAIT_MS)?;
            Ok(())
        })
        .map_err(|_| "fixed-peer-receiver-spawn")?;

    let (sender_state, sender_ready, sender_arm, sender_finished, sender_terminal) = (
        state.clone(),
        ready.clone(),
        arm_sender.clone(),
        finished.clone(),
        terminal_sender.clone(),
    );
    let sender_clock = Clock {
        hz: clock.hz,
        source: clock.source,
        bits: clock.bits,
        resolution_ns: clock.resolution_ns,
    };
    let sender = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            // Reserve sample storage before making the peer runnable.
            let mut report = Report::new(config);
            let configured = support::configure(SENDER_PRIORITY);
            if let Ok(actual) = configured {
                sender_state
                    .sender_policy
                    .store(actual.policy, Ordering::Relaxed);
                sender_state
                    .sender_priority
                    .store(actual.priority, Ordering::Relaxed);
                sender_state
                    .sender_tid
                    .store(native::tid(), Ordering::Release);
                report.sender = actual;
                report.sender_tid = native::tid();
            }
            sender_ready.post()?;
            configured?;
            sender_arm.wait(WAIT_MS)?;
            while !sender_state.active.load(Ordering::Acquire) {
                if sender_state.cancelled.load(Ordering::Acquire) {
                    return Ok(report);
                }
                thread::yield_now();
            }
            let tid = sender_state.receiver_tid.load(Ordering::Acquire);
            report.receiver_tid = tid;
            report.receiver = Schedule {
                policy: sender_state.receiver_policy.load(Ordering::Relaxed),
                priority: sender_state.receiver_priority.load(Ordering::Relaxed),
            };
            if tid <= 0
                || tid == report.sender_tid
                || report.sender.policy != 1
                || report.receiver.policy != 1
                || report.sender.priority != SENDER_PRIORITY
                || report.receiver.priority != report.sender.priority
            {
                return Err("fixed-peer-scheduling-contract");
            }
            for sequence in 1..=config.total() {
                if sender_state.cancelled.load(Ordering::Acquire) {
                    return Ok(report);
                }
                let (state, priority) = native::state(tid);
                if state != State::Ready || priority != SENDER_PRIORITY {
                    return Err("fixed-peer-not-ready");
                }
                let window_begin = native::monotonic_ns()?;
                let start = native::counter();
                sender_state.turn.store(sequence, Ordering::Release);
                thread::yield_now();
                let admission_end = native::counter();
                let window_end = native::monotonic_ns()?;
                verify_peer_marker(&sender_state, sequence, tid)?;
                let receive = sender_state.ticks.load(Ordering::Relaxed);
                let window_ns = sender_clock.window(
                    window_begin,
                    window_end,
                    &[
                        admission_end.wrapping_sub(start),
                        receive.wrapping_sub(start),
                    ],
                )?;
                report.peer_ready_verified = true;
                report.counts.attempted += 1;
                report.counts.admitted += 1;
                report.counts.handled += 1;
                report.checksum = report.checksum.wrapping_add(u64::from(sequence));
                if sequence > config.warmup {
                    report.retained_checksum =
                        report.retained_checksum.wrapping_add(u64::from(sequence));
                    report.samples.push(Sample {
                        sequence,
                        start,
                        admission_end,
                        receive,
                        window_ns,
                        peer_tid: tid,
                        ..Sample::default()
                    });
                }
            }
            sender_state.sender_terminal.store(true, Ordering::Release);
            sender_finished.post()?;
            sender_terminal.wait(WAIT_MS)?;
            Ok(report)
        });
    let sender = match sender {
        Ok(handle) => handle,
        Err(_) => {
            state.cancelled.store(true, Ordering::Release);
            let armed = arm_receiver.post();
            let released = terminal_receiver.post();
            let joined = support::join_bounded(receiver);
            armed?;
            released?;
            joined?;
            return Err("fixed-peer-sender-spawn");
        }
    };

    let measurement = (|| {
        ready.wait(WAIT_MS)?;
        ready.wait(WAIT_MS)?;
        if state.sender_tid.load(Ordering::Acquire) <= 0
            || state.receiver_tid.load(Ordering::Acquire) <= 0
        {
            return Err("fixed-peer-startup");
        }
        arm_receiver.post()?;
        arm_sender.post()?;
        finished.wait(WAIT_MS)?;
        support::wait_until(|| {
            state.sender_terminal.load(Ordering::Acquire)
                && state.receiver_terminal.load(Ordering::Acquire)
        })?;
        let policy = Schedule {
            policy: 1,
            priority: SENDER_PRIORITY,
        };
        let sender_held =
            support::blocked(state.sender_tid.load(Ordering::Acquire), policy, || {
                state.sender_terminal.load(Ordering::Acquire)
            })?;
        let receiver_held =
            support::blocked(state.receiver_tid.load(Ordering::Acquire), policy, || {
                state.receiver_terminal.load(Ordering::Acquire)
            })?;
        Ok(sender_held && receiver_held)
    })();
    state.cancelled.store(true, Ordering::Release);
    let sender_armed = arm_sender.post();
    let receiver_armed = arm_receiver.post();
    let sender_released = terminal_sender.post();
    let receiver_released = terminal_receiver.post();
    let sender_joined = support::join_bounded(sender);
    let receiver_joined = support::join_bounded(receiver);
    let terminal_verified = measurement?;
    sender_armed?;
    receiver_armed?;
    sender_released?;
    receiver_released?;
    receiver_joined?;
    let mut report = sender_joined?;
    report.terminal_quiescence_verified = terminal_verified;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn early_publication_completes_without_needing_a_park() {
        let state = NotificationState::new();
        state.published.store(1, Ordering::Release);
        let wake = park_predicate(&state, 1, || panic!("early event must not park")).unwrap();
        assert_eq!(wake.returns, 0);
    }

    #[test]
    fn stale_and_spurious_returns_do_not_complete_an_unpublished_sequence() {
        let state = NotificationState::new();
        state.published.store(3, Ordering::Release);
        let mut parks = 0;
        let wake = park_predicate(&state, 4, || {
            parks += 1;
            if parks == 4 {
                state.published.store(4, Ordering::Release);
            }
        })
        .unwrap();
        assert_eq!((parks, wake.returns), (4, 4));
    }

    #[test]
    fn publication_between_predicate_and_park_cannot_lose_the_token() {
        let state = NotificationState::new();
        let wake = park_predicate(&state, 1, || {
            state.published.store(1, Ordering::Release);
            thread::current().unpark();
            thread::park();
        })
        .unwrap();
        assert_eq!(wake.returns, 1);
    }

    #[test]
    fn coalesced_unparks_and_leftover_token_do_not_replay_work() {
        let state = NotificationState::new();
        thread::current().unpark();
        thread::current().unpark();
        state.published.store(1, Ordering::Release);
        assert_eq!(park_predicate(&state, 1, thread::park).unwrap().returns, 0);
        let mut parks = 0;
        let wake = park_predicate(&state, 2, || {
            parks += 1;
            if parks == 2 {
                state.published.store(2, Ordering::Release);
                thread::current().unpark();
            }
            thread::park();
        })
        .unwrap();
        assert_eq!((parks, wake.returns), (2, 2));
    }

    #[test]
    fn cancellation_wakes_a_real_parked_thread_and_joins() {
        let state = Arc::new(NotificationState::new());
        let entered = Arc::new(AtomicBool::new(false));
        let (worker, worker_entered) = (state.clone(), entered.clone());
        let handle = thread::spawn(move || {
            park_predicate(&worker, 1, || {
                worker_entered.store(true, Ordering::Release);
                thread::park();
            })
            .map(|_| ())
        });
        support::wait_until(|| entered.load(Ordering::Acquire)).unwrap();
        state.cancelled.store(true, Ordering::Release);
        handle.thread().unpark();
        assert_eq!(support::join_bounded(handle), Err("notification-cancelled"));
    }

    #[test]
    fn skipped_sequences_are_refused_instead_of_counted() {
        let state = NotificationState::new();
        state.published.store(2, Ordering::Release);
        assert!(matches!(
            park_predicate(&state, 1, || panic!("must reject")),
            Err("park-sequence-skipped")
        ));
    }

    #[test]
    fn notification_controls_preserve_all_sequences_and_final_hold() {
        let config = Config::new(16, 4).unwrap();
        let clock = Clock::new().unwrap();
        for report in [
            semaphore(config, &clock).unwrap(),
            park(config, &clock).unwrap(),
        ] {
            assert_eq!(report.samples.len(), 16);
            assert_eq!(report.counts.attempted, 20);
            assert_eq!(report.counts.admitted, 20);
            assert_eq!(report.counts.handled, 20);
            assert_eq!(report.counts.rejected, 0);
            assert_eq!(report.checksum, 210);
            assert_eq!(report.retained_checksum, 200);
            assert_eq!(report.samples.first().unwrap().sequence, 5);
            assert_eq!(report.samples.last().unwrap().sequence, 20);
            assert_ne!(report.sender_tid, report.receiver_tid);
            assert!(report
                .samples
                .iter()
                .all(|sample| sample.peer_tid == report.receiver_tid));
            if !native::is_nuttx() {
                assert!(!report.terminal_quiescence_verified);
                assert!(report.samples.iter().all(|sample| !sample.blocked));
            }
        }
    }

    #[test]
    fn failed_early_and_partial_runs_release_both_notification_waits() {
        let config = Config::new(8, 0).unwrap();
        let clock = Clock::new().unwrap();
        for fail_at in [1, 4] {
            let fail = |sequence| {
                if sequence == fail_at {
                    Err("injected-before-notify")
                } else {
                    Ok(())
                }
            };
            assert!(matches!(
                notification(config, &clock, Park, fail),
                Err("injected-before-notify")
            ));
            assert!(matches!(
                notification(config, &clock, Semaphore(Gate::new().unwrap()), fail),
                Err("injected-before-notify")
            ));
        }
    }

    #[test]
    fn missing_or_wrong_fixed_peer_marker_is_refused() {
        let state = PeerState::new();
        assert_eq!(
            verify_peer_marker(&state, 1, 42),
            Err("yield-did-not-run-peer")
        );
        state.completed.store(1, Ordering::Release);
        state.observed_tid.store(43, Ordering::Relaxed);
        assert_eq!(
            verify_peer_marker(&state, 1, 42),
            Err("yield-peer-identity")
        );
        state.observed_tid.store(42, Ordering::Relaxed);
        assert_eq!(verify_peer_marker(&state, 1, 42), Ok(()));
        assert_eq!(
            verify_peer_marker(&state, 2, 42),
            Err("yield-did-not-run-peer")
        );
    }

    #[test]
    fn fixed_peer_never_promotes_a_host_yield_to_fifo_evidence() {
        if !native::is_nuttx() {
            assert!(matches!(
                fixed_peer(Config::new(1, 0).unwrap(), &Clock::new().unwrap()),
                Err("fixed-peer-requires-single-core-fifo")
            ));
        }
    }

    #[test]
    fn a_semaphore_notification_without_publication_is_refused() {
        let state = NotificationState::new();
        let notification = Semaphore(Gate::new().unwrap());
        notification.notify(&thread::current()).unwrap();
        assert!(matches!(
            notification.wait(&state, 1),
            Err("semaphore-sequence")
        ));
    }
}

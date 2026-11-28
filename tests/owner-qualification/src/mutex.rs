//! A finite H/M/L witness for the target's actual `std::sync::Mutex` backend.
//! Setup gates use park/unpark notifications, never a second resource mutex.
//! Host execution covers ownership and orchestration, not priority inheritance.

use crate::platform::{self, MutexConfiguration, ThreadState};
use crate::scenarios::{join, spawn, until};
use crate::Outcome;
use serde::Serialize;
use std::hint::black_box;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const LOW: i32 = 120;
const MEDIUM: i32 = 150;
const HIGH: i32 = 180;
const GATE_DEADLINE: Duration = Duration::from_secs(4);
const MEDIUM_WINDOW: Duration = Duration::from_millis(25);

#[derive(Serialize)]
pub struct Report {
    expected_inheritance: bool,
    native: Option<NativeEvidence>,
    order: Order,
    high_wait_us: u64,
    medium_window_us: u64,
    medium_elapsed_us: u64,
    medium_iterations: u64,
    high_completed_at_medium_start: bool,
    high_completed_at_medium_finish: bool,
    final_value: u32,
}

#[derive(Serialize)]
struct NativeEvidence {
    configuration: MutexConfiguration,
    low_before_high: ThreadState,
    medium_before_high: ThreadState,
    high_wait: ThreadState,
    low_while_high_waits: ThreadState,
    low_ready: ThreadState,
    medium_ready: ThreadState,
    low_before_unlock: ThreadState,
    low_after_unlock: ThreadState,
}

#[derive(Serialize)]
struct Order {
    // Immediately before drop(guard); H can preempt inside the unlock call.
    low_unlock: usize,
    high_acquire: usize,
    medium_start: usize,
    medium_finish: usize,
}

#[derive(Default)]
struct Shared {
    value: Mutex<u32>,
    release: AtomicBool,
    low_gate: AtomicBool,
    medium_gate: AtomicBool,
    high_attempt: AtomicBool,
    low_done: AtomicBool,
    medium_done: AtomicBool,
    high_done: AtomicBool,
    low_tid: AtomicI32,
    medium_tid: AtomicI32,
    high_tid: AtomicI32,
    sequence: AtomicUsize,
}

impl Shared {
    fn stamp(&self) -> usize {
        self.sequence.fetch_add(1, SeqCst) + 1
    }
}

fn gate(release: &AtomicBool, entered: &AtomicBool) {
    let deadline = Instant::now() + GATE_DEADLINE;
    entered.store(true, SeqCst);
    while !release.load(SeqCst) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "mutex setup gate deadline");
        // A timeout/spurious wake cannot release this gate. The notification
        // semaphore in the target parker uses PRIO_NONE, without an owner.
        thread::park_timeout(remaining);
    }
}

fn observe_wait(
    tid: &AtomicI32,
    entered: &AtomicBool,
    priority: i32,
    outcome: &mut Outcome,
) -> Option<ThreadState> {
    outcome.required_waits += 1;
    until(|| {
        entered.load(SeqCst) && (!platform::NATIVE || platform::waiting(tid.load(SeqCst), priority))
    });
    if platform::NATIVE {
        outcome.native_waits += 1;
    }
    platform::thread_state(tid.load(SeqCst))
}

fn assert_priority(state: ThreadState, base: i32, effective: i32) {
    assert_eq!(state.policy, platform::fifo_policy().unwrap());
    assert_eq!(state.base_priority, base);
    assert_eq!(state.effective_priority, effective);
}

pub fn run(expected_inheritance: bool) -> Outcome {
    let configuration = platform::mutex_configuration();
    if let Some(config) = configuration {
        // Keep kernel support on in both builds: only the default protocol
        // changes. The runtime observation must agree with retained Kconfig.
        assert!(config.priority_inheritance);
        assert_eq!(config.holder_capacity, 0);
        assert_eq!(config.default_prio_inherit, expected_inheritance);
        assert_eq!(
            config.default_protocol,
            if expected_inheritance {
                config.protocol_inherit
            } else {
                config.protocol_none
            }
        );
        assert_eq!(platform::schedule().priority, Some(220));
    }

    let mut outcome = Outcome::default();
    let shared = Arc::new(Shared::default());
    let low = spawn("mutex-low", LOW, {
        let shared = shared.clone();
        move |tid| {
            shared.low_tid.store(tid, SeqCst);
            let mut value = shared.value.lock().unwrap();
            *value = 41;
            gate(&shared.release, &shared.low_gate);
            let before = platform::thread_state(tid);
            let release_order = shared.stamp();
            drop(value);
            let after = platform::thread_state(tid);
            shared.low_done.store(true, SeqCst);
            (release_order, before, after)
        }
    });
    let medium = spawn("mutex-medium", MEDIUM, {
        let shared = shared.clone();
        move |tid| {
            shared.medium_tid.store(tid, SeqCst);
            gate(&shared.release, &shared.medium_gate);
            let start_order = shared.stamp();
            let high_at_start = shared.high_done.load(SeqCst);
            let started = Instant::now();
            let mut iterations = 0_u64;
            while started.elapsed() < MEDIUM_WINDOW {
                iterations += 1;
                black_box(iterations);
            }
            let elapsed_us = started.elapsed().as_micros() as u64;
            let high_at_finish = shared.high_done.load(SeqCst);
            let finish_order = shared.stamp();
            shared.medium_done.store(true, SeqCst);
            (
                start_order,
                finish_order,
                high_at_start,
                high_at_finish,
                elapsed_us,
                iterations,
            )
        }
    });

    let low_before_high = observe_wait(&shared.low_tid, &shared.low_gate, LOW, &mut outcome);
    let medium_before_high = observe_wait(
        &shared.medium_tid,
        &shared.medium_gate,
        MEDIUM,
        &mut outcome,
    );
    let high = spawn("mutex-high", HIGH, {
        let shared = shared.clone();
        move |tid| {
            shared.high_tid.store(tid, SeqCst);
            let started = Instant::now();
            // No logging, allocation or other blocking operation between this
            // marker and lock: an observed WAIT_SEM is this mutex acquisition.
            shared.high_attempt.store(true, SeqCst);
            let mut value = shared.value.lock().unwrap();
            let acquired_order = shared.stamp();
            let wait_us = started.elapsed().as_micros() as u64;
            let observed = *value;
            *value = 42;
            drop(value);
            assert_eq!(observed, 41);
            shared.high_done.store(true, SeqCst);
            (acquired_order, wait_us)
        }
    });
    let high_wait = observe_wait(&shared.high_tid, &shared.high_attempt, HIGH, &mut outcome);
    let low_while_high_waits = platform::thread_state(shared.low_tid.load(SeqCst));
    assert!(!shared.high_done.load(SeqCst));

    // Coordinator FIFO220 keeps the CPU while both workers become runnable.
    // This avoids relying on two sleep timers expiring in a favorable order.
    shared.release.store(true, SeqCst);
    low.thread().unpark();
    medium.thread().unpark();
    let low_ready = platform::thread_state(shared.low_tid.load(SeqCst));
    let medium_ready = platform::thread_state(shared.medium_tid.load(SeqCst));

    // Actually sleep, never yield to equal-priority peers or join a worker
    // while this test mutex is held. The coordinator's finite polling is part
    // of this fixture, so elapsed observations are not product timing bounds.
    until(|| {
        shared.low_done.load(SeqCst)
            && shared.medium_done.load(SeqCst)
            && shared.high_done.load(SeqCst)
    });
    let (low_unlock, low_before_unlock, low_after_unlock) = join(low, &mut outcome);
    let (medium_start, medium_finish, high_at_start, high_at_finish, elapsed_us, iterations) =
        join(medium, &mut outcome);
    let (high_acquire, high_wait_us) = join(high, &mut outcome);
    assert_eq!(shared.sequence.load(SeqCst), 4);
    assert!(low_unlock < high_acquire);
    assert!(medium_start < medium_finish);
    assert!(iterations > 0);
    assert!(elapsed_us >= MEDIUM_WINDOW.as_micros() as u64);
    let final_value = *shared.value.lock().unwrap();
    assert_eq!(final_value, 42);

    let native = configuration.map(|configuration| {
        let evidence = NativeEvidence {
            configuration,
            low_before_high: low_before_high.unwrap(),
            medium_before_high: medium_before_high.unwrap(),
            high_wait: high_wait.unwrap(),
            low_while_high_waits: low_while_high_waits.unwrap(),
            low_ready: low_ready.unwrap(),
            medium_ready: medium_ready.unwrap(),
            low_before_unlock: low_before_unlock.unwrap(),
            low_after_unlock: low_after_unlock.unwrap(),
        };
        let effective_low = if expected_inheritance { HIGH } else { LOW };
        assert_priority(evidence.low_before_high, LOW, LOW);
        assert_priority(evidence.medium_before_high, MEDIUM, MEDIUM);
        assert_priority(evidence.high_wait, HIGH, HIGH);
        assert_priority(evidence.low_while_high_waits, LOW, effective_low);
        assert_priority(evidence.low_ready, LOW, effective_low);
        assert_priority(evidence.medium_ready, MEDIUM, MEDIUM);
        assert_priority(evidence.low_before_unlock, LOW, effective_low);
        assert_priority(evidence.low_after_unlock, LOW, LOW);
        for state in [
            evidence.low_before_high,
            evidence.medium_before_high,
            evidence.high_wait,
            evidence.low_while_high_waits,
        ] {
            assert!(state.waiting_semaphore && !state.runnable);
        }
        for state in [
            evidence.low_ready,
            evidence.medium_ready,
            evidence.low_before_unlock,
            evidence.low_after_unlock,
        ] {
            assert!(state.runnable && !state.waiting_semaphore);
        }
        assert_eq!(
            [low_unlock, high_acquire, medium_start, medium_finish],
            if expected_inheritance {
                [1, 2, 3, 4]
            } else {
                [3, 4, 1, 2]
            }
        );
        assert_eq!(high_at_start, expected_inheritance);
        assert_eq!(high_at_finish, expected_inheritance);
        evidence
    });
    outcome.endpoints_closed = true;
    outcome.mutex = Some(Report {
        expected_inheritance,
        native,
        order: Order {
            low_unlock,
            high_acquire,
            medium_start,
            medium_finish,
        },
        high_wait_us,
        medium_window_us: MEDIUM_WINDOW.as_micros() as u64,
        medium_elapsed_us: elapsed_us,
        medium_iterations: iterations,
        high_completed_at_medium_start: high_at_start,
        high_completed_at_medium_finish: high_at_finish,
        final_value,
    });
    outcome
}

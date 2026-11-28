//! Simultaneous and nested ownership of actual std mutexes on the target.
//!
//! Nine distinct contended mutexes exceed the old eight-record global pool.
//! Releasing them from highest to lowest waiter priority must retain donations
//! from the remaining locks. A second round verifies that holder records can
//! be reused. All setup gates are PRIO_NONE park/unpark notifications.

use crate::platform::{self, HolderState, MutexConfiguration, ThreadState};
use crate::scenarios::{join, spawn, until};
use crate::Outcome;
use serde::Serialize;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const LOCKS: usize = 9;
const ROUNDS: usize = 2;
const OWNER_PRIORITY: i32 = 120;
const FIRST_WAITER_PRIORITY: i32 = 140;
const PRIORITY_STEP: i32 = 5;
const GATE_DEADLINE: Duration = Duration::from_secs(4);
const WAITER_ROLES: [&str; LOCKS] = [
    "mutex-nested-waiter-0",
    "mutex-nested-waiter-1",
    "mutex-nested-waiter-2",
    "mutex-nested-waiter-3",
    "mutex-nested-waiter-4",
    "mutex-nested-waiter-5",
    "mutex-nested-waiter-6",
    "mutex-nested-waiter-7",
    "mutex-nested-waiter-8",
];

#[derive(Serialize)]
pub struct Report {
    lock_count: usize,
    rounds: Vec<Round>,
    native_configuration: Option<MutexConfiguration>,
    handoffs: usize,
    final_values: [u32; LOCKS],
}

#[derive(Serialize)]
struct Round {
    round: usize,
    initial: Option<HolderState>,
    contended: Vec<Contention>,
    released: Vec<Release>,
}

#[derive(Serialize)]
struct Contention {
    lock_index: usize,
    waiter: Option<ThreadState>,
    owner: Option<HolderState>,
}

#[derive(Serialize)]
struct Release {
    lock_index: usize,
    owner: Option<HolderState>,
}

struct Shared {
    values: [Mutex<u32>; LOCKS],
    owner_tid: AtomicI32,
    waiter_tids: [AtomicI32; LOCKS],
    start_round: AtomicUsize,
    owner_holding: AtomicUsize,
    start_waiters: [AtomicUsize; LOCKS],
    waiter_attempts: [AtomicUsize; LOCKS],
    waiter_completions: [AtomicUsize; LOCKS],
    release_step: AtomicUsize,
    owner_released: AtomicUsize,
    handoffs: AtomicUsize,
    finish: AtomicBool,
}

impl Shared {
    fn new() -> Self {
        Self {
            values: std::array::from_fn(|_| Mutex::new(0)),
            owner_tid: AtomicI32::new(0),
            waiter_tids: std::array::from_fn(|_| AtomicI32::new(0)),
            start_round: AtomicUsize::new(0),
            owner_holding: AtomicUsize::new(0),
            start_waiters: std::array::from_fn(|_| AtomicUsize::new(0)),
            waiter_attempts: std::array::from_fn(|_| AtomicUsize::new(0)),
            waiter_completions: std::array::from_fn(|_| AtomicUsize::new(0)),
            release_step: AtomicUsize::new(0),
            owner_released: AtomicUsize::new(0),
            handoffs: AtomicUsize::new(0),
            finish: AtomicBool::new(false),
        }
    }
}

fn waiter_priority(index: usize) -> i32 {
    FIRST_WAITER_PRIORITY + PRIORITY_STEP * index as i32
}

fn gate(mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + GATE_DEADLINE;
    while !ready() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "nested mutex phase deadline");
        // Spurious or timeout wakeups do not open the phase gate.
        thread::park_timeout(remaining);
    }
}

fn observe_wait(
    tid: &AtomicI32,
    priority: i32,
    mut phase: impl FnMut() -> bool,
    outcome: &mut Outcome,
) -> Option<ThreadState> {
    outcome.required_waits += 1;
    until(|| phase() && (!platform::NATIVE || platform::waiting(tid.load(SeqCst), priority)));
    if platform::NATIVE {
        outcome.native_waits += 1;
    }
    platform::thread_state(tid.load(SeqCst))
}

fn owner_state(shared: &Shared, holders: usize, priority: i32) -> Option<HolderState> {
    let observed = platform::holder_state(shared.owner_tid.load(SeqCst));
    if let Some(observed) = observed {
        assert_eq!(observed.tracked_holders, holders);
        assert_eq!(observed.state.base_priority, OWNER_PRIORITY);
        assert_eq!(observed.state.effective_priority, priority);
        assert_eq!(observed.state.policy, platform::fifo_policy().unwrap());
        assert!(observed.state.waiting_semaphore && !observed.state.runnable);
    }
    observed
}

fn ordered_lock_smoke() {
    use lock_order::{scope, Mutex, Next, ScopeError, Unlocked};
    use std::cell::Cell;

    type First = Next<Unlocked>;
    type Second = Next<First>;
    let first = Mutex::<_, First>::new(0_u32);
    let second = Mutex::<_, Second>::new(0_u32);
    let nested_ran = Cell::new(false);
    scope(|root| {
        // Initialize each ordinary native mutex before holding them together.
        first.with_lock(root, |_, _| ()).unwrap();
        second.with_lock(root, |_, _| ()).unwrap();
        first
            .with_lock(root, |held, value| {
                *value = 1;
                assert_eq!(
                    scope(|_| nested_ran.set(true)),
                    Err(ScopeError::AlreadyActive)
                );
                second
                    .with_lock(held, |_, result| *result = *value + 40)
                    .unwrap();
            })
            .unwrap();
    })
    .unwrap();
    assert!(!nested_ran.get());
    // A completed root releases the TLS admission flag on this actual target.
    scope(|root| {
        assert_eq!(first.with_lock(root, |_, value| *value).unwrap(), 1);
        assert_eq!(second.with_lock(root, |_, value| *value).unwrap(), 41);
    })
    .unwrap();
}

pub fn run() -> Outcome {
    let configuration = platform::mutex_configuration();
    if let Some(configuration) = configuration {
        assert!(configuration.priority_inheritance);
        assert!(configuration.default_prio_inherit);
        assert_eq!(
            configuration.default_protocol,
            configuration.protocol_inherit
        );
        assert_eq!(configuration.holder_capacity, 0);
        assert_eq!(platform::schedule().priority, Some(220));
    }

    let mut outcome = Outcome::default();
    let shared = Arc::new(Shared::new());
    // Initialize the native std mutex allocations before any resource is held.
    for mutex in &shared.values {
        drop(mutex.lock().unwrap());
    }
    let owner = spawn("mutex-nested-owner", OWNER_PRIORITY, {
        let shared = shared.clone();
        move |tid| {
            shared.owner_tid.store(tid, SeqCst);
            for round in 1..=ROUNDS {
                gate(|| shared.start_round.load(SeqCst) >= round);
                // Fixed guard storage: no allocation, logging or unrelated
                // resource lock occurs while the nested resources are held.
                let mut guards: [Option<_>; LOCKS] = std::array::from_fn(|index| {
                    let mut value = shared.values[index].lock().unwrap();
                    *value += 10;
                    Some(value)
                });
                shared.owner_holding.store(round, SeqCst);
                for released in 1..=LOCKS {
                    let step = (round - 1) * LOCKS + released;
                    gate(|| shared.release_step.load(SeqCst) >= step);
                    drop(guards[LOCKS - released].take().unwrap());
                    // Handoff may preempt inside drop; publish only afterward.
                    shared.owner_released.store(step, SeqCst);
                }
            }
            // Keep the TCB available for the final base-priority/zero-holder
            // observation before the coordinator permits shutdown and joins.
            gate(|| shared.finish.load(SeqCst));
        }
    });
    let mut waiters = Vec::with_capacity(LOCKS);
    for (index, role) in WAITER_ROLES.into_iter().enumerate() {
        let shared = shared.clone();
        waiters.push(spawn(role, waiter_priority(index), move |tid| {
            shared.waiter_tids[index].store(tid, SeqCst);
            for round in 1..=ROUNDS {
                gate(|| shared.start_waiters[index].load(SeqCst) >= round);
                shared.waiter_attempts[index].store(round, SeqCst);
                let mut value = shared.values[index].lock().unwrap();
                *value += 1;
                drop(value);
                shared.handoffs.fetch_add(1, SeqCst);
                shared.waiter_completions[index].store(round, SeqCst);
            }
        }));
    }

    // Prewarm every park/identity path before starting the resource experiment.
    observe_wait(
        &shared.owner_tid,
        OWNER_PRIORITY,
        || !platform::NATIVE || shared.owner_tid.load(SeqCst) > 0,
        &mut outcome,
    );
    for index in 0..LOCKS {
        observe_wait(
            &shared.waiter_tids[index],
            waiter_priority(index),
            || !platform::NATIVE || shared.waiter_tids[index].load(SeqCst) > 0,
            &mut outcome,
        );
    }

    let mut rounds = Vec::with_capacity(ROUNDS);
    for round in 1..=ROUNDS {
        let mut contended = Vec::with_capacity(LOCKS);
        let mut released = Vec::with_capacity(LOCKS);
        shared.start_round.store(round, SeqCst);
        owner.thread().unpark();
        observe_wait(
            &shared.owner_tid,
            OWNER_PRIORITY,
            || shared.owner_holding.load(SeqCst) == round,
            &mut outcome,
        );
        let initial = owner_state(&shared, 0, OWNER_PRIORITY);

        for (index, waiter) in waiters.iter().enumerate() {
            shared.start_waiters[index].store(round, SeqCst);
            waiter.thread().unpark();
            let state = observe_wait(
                &shared.waiter_tids[index],
                waiter_priority(index),
                || shared.waiter_attempts[index].load(SeqCst) == round,
                &mut outcome,
            );
            assert!(shared.waiter_completions[index].load(SeqCst) < round);
            if let Some(state) = state {
                assert_eq!(state.base_priority, waiter_priority(index));
                assert_eq!(state.effective_priority, waiter_priority(index));
                assert_eq!(state.policy, platform::fifo_policy().unwrap());
                assert!(state.waiting_semaphore && !state.runnable);
            }
            contended.push(Contention {
                lock_index: index,
                waiter: state,
                owner: owner_state(&shared, index + 1, waiter_priority(index)),
            });
        }

        for index in (0..LOCKS).rev() {
            let step = (round - 1) * LOCKS + LOCKS - index;
            let remaining_priority = if index == 0 {
                OWNER_PRIORITY
            } else {
                waiter_priority(index - 1)
            };
            shared.release_step.store(step, SeqCst);
            owner.thread().unpark();
            observe_wait(
                &shared.owner_tid,
                remaining_priority,
                || {
                    shared.owner_released.load(SeqCst) == step
                        && shared.waiter_completions[index].load(SeqCst) == round
                },
                &mut outcome,
            );
            released.push(Release {
                lock_index: index,
                owner: owner_state(&shared, index, remaining_priority),
            });
        }
        rounds.push(Round {
            round,
            initial,
            contended,
            released,
        });
    }

    shared.finish.store(true, SeqCst);
    owner.thread().unpark();
    join(owner, &mut outcome);
    for waiter in waiters {
        join(waiter, &mut outcome);
    }
    let handoffs = shared.handoffs.load(SeqCst);
    let final_values = std::array::from_fn(|index| *shared.values[index].lock().unwrap());
    assert_eq!(handoffs, LOCKS * ROUNDS);
    assert_eq!(final_values, [11 * ROUNDS as u32; LOCKS]);
    // Exercise the public ordering/TLS contract after the measurement workers
    // have joined. These assertions are part of this source-bound case's pass.
    ordered_lock_smoke();
    outcome.attempted = handoffs;
    outcome.admitted = handoffs;
    outcome.handled = handoffs;
    outcome.endpoints_closed = true;
    outcome.mutex_nested = Some(Report {
        lock_count: LOCKS,
        rounds,
        native_configuration: configuration,
        handoffs,
        final_values,
    });
    outcome
}

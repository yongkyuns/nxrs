//! Finite RAM accounting controls, not a latency benchmark or production runtime.
#![deny(unsafe_op_in_unsafe_fn)]

mod platform;
mod transport;
pub use transport::{CrossbeamSelect, StdInbox, Transport};

use nxrs_allocation_probe::{Snapshot, Tracking};
use std::alloc::System;
use std::mem::size_of;
use std::sync::{Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use transport::ReceiveError;

#[global_allocator]
static ALLOCATOR: Tracking<System> = Tracking::new(System);

const MAX_WORKERS: usize = 20;
const LANES: usize = 3;
const SLOTS: usize = 8;
const REPEATED_WAITS: usize = 8;
const WAIT: Duration = Duration::from_millis(2);
const CONTROL_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Event([u32; 16]);
const _: () = assert!(size_of::<Event>() == 64);

impl Event {
    fn new(owner: usize, lane: usize, sequence: usize) -> Self {
        let mut words = [0; 16];
        words[0] = owner as u32;
        words[1] = lane as u32;
        words[2] = sequence as u32;
        for (index, word) in words.iter_mut().enumerate().skip(3) {
            *word = (owner as u32).wrapping_mul(0x51ed270b)
                ^ (lane as u32).wrapping_mul(0x9e3779b9)
                ^ (sequence as u32).rotate_left(index as u32)
                ^ index as u32;
        }
        Self(words)
    }

    fn verify(self, owner: usize, next: &mut [usize; LANES]) {
        let lane = self.0[1] as usize;
        assert!(lane < LANES, "invalid event lane");
        assert_eq!(
            self,
            Self::new(owner, lane, next[lane]),
            "payload/order mismatch"
        );
        next[lane] += 1;
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Case {
    Baseline,
    Threads,
    Queues,
    Services,
}

impl Case {
    fn name(self) -> &'static str {
        match self {
            Self::Baseline => "baseline",
            Self::Threads => "threads",
            Self::Queues => "queues",
            Self::Services => "services",
        }
    }
}

struct Config {
    case: Case,
    owners: usize,
    stack: usize,
}

fn arguments(thread_only: bool) -> Config {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(
        arguments.len(),
        3,
        "usage: <threads|queues|services> <0|1|5|10|20> <stack-bytes>"
    );
    let case = match arguments[0].as_str() {
        "threads" if thread_only => Case::Threads,
        "queues" if !thread_only => Case::Queues,
        "services" if !thread_only => Case::Services,
        _ => panic!("case is not supported by this binary"),
    };
    let owners = arguments[1].parse().expect("invalid worker/owner count");
    assert!(
        [0, 1, 5, 10, 20].contains(&owners),
        "use a declared scaling count"
    );
    let stack = arguments[2].parse().expect("invalid stack size");
    assert!(stack > 0, "an explicit nonzero stack request is required");
    Config {
        case,
        owners,
        stack,
    }
}

#[derive(Default)]
struct GateState {
    stage: usize,
    parked: usize,
}
static GATE: (Mutex<GateState>, Condvar) = (
    Mutex::new(GateState {
        stage: 0,
        parked: 0,
    }),
    Condvar::new(),
);

fn checkpoint(stage: usize) {
    let mut state = GATE.0.lock().unwrap();
    assert_eq!(state.stage, stage);
    state.parked += 1;
    GATE.1.notify_all();
    while state.stage == stage {
        state = GATE.1.wait(state).unwrap();
    }
    assert_eq!(state.stage, stage + 1);
}

fn wait_parked(stage: usize, workers: usize) {
    let deadline = Instant::now() + CONTROL_TIMEOUT;
    let mut state = GATE.0.lock().unwrap();
    assert_eq!(state.stage, stage);
    while state.parked != workers {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "workers did not reach the diagnostic gate"
        );
        state = GATE.1.wait_timeout(state, remaining).unwrap().0;
    }
    // Each worker increments parked while holding this mutex and then waits.
    // Acquiring it here proves that all have released it into the gate wait.
}

fn release(stage: usize) {
    let mut state = GATE.0.lock().unwrap();
    assert_eq!(state.stage, stage);
    state.parked = 0;
    state.stage += 1;
    GATE.1.notify_all();
}

#[derive(Clone, Copy, Default)]
struct Sample {
    phase: &'static str,
    rust: Snapshot,
    heap: platform::NativeHeap,
    main_stack: platform::Stack,
}

#[derive(Clone, Copy, Default)]
struct WorkerResult {
    received: usize,
    timeouts: usize,
    stack: platform::Stack,
}

struct Record {
    samples: [Sample; 12],
    length: usize,
    workers: [WorkerResult; MAX_WORKERS],
    joined: usize,
    endpoint_wiring_bytes: usize,
    join_handle_storage_bytes: usize,
}

impl Record {
    fn begin() -> Self {
        // These fixed runtime conveniences are deliberately initialized once
        // before baseline. No arbitrary channel or worker warm-up is performed.
        nxrs_allocation_probe::begin_measurement();
        drop(thread::current());
        *GATE.0.lock().unwrap() = GateState::default();
        Self {
            samples: [Sample::default(); 12],
            length: 0,
            workers: [WorkerResult::default(); MAX_WORKERS],
            joined: 0,
            endpoint_wiring_bytes: 0,
            join_handle_storage_bytes: 0,
        }
    }

    fn sample(&mut self, phase: &'static str) {
        assert!(self.length < self.samples.len());
        let sample = Sample {
            phase,
            heap: platform::heap(),
            main_stack: platform::stack(),
            rust: ALLOCATOR.snapshot(),
        };
        if self.length != 0 {
            sample
                .rust
                .validate_after(self.samples[self.length - 1].rust)
                .unwrap();
        }
        assert!(!sample.rust.invalid);
        assert_eq!(
            sample.rust.failed, 0,
            "allocator failure invalidates this process"
        );
        self.samples[self.length] = sample;
        self.length += 1;
    }

    fn join(&mut self, handles: Vec<JoinHandle<WorkerResult>>, messages: usize, waits: usize) {
        for handle in handles {
            let result = handle.join().expect("worker panicked");
            assert_eq!(result.received, messages);
            assert_eq!(result.timeouts, waits);
            if platform::NATIVE {
                assert!(result.stack.usable > 0);
                assert!(result.stack.observed > 0);
                assert!(result.stack.observed <= result.stack.usable);
            }
            self.workers[self.joined] = result;
            self.joined += 1;
        }
    }

    fn emit(&self, config: &Config, transport: &str, queues: usize, capacity: usize) {
        let first = self.samples[0];
        let last = self.samples[self.length - 1];
        // Reporting starts only after all measurements and owned-resource cleanup.
        let workers = if matches!(config.case, Case::Threads | Case::Services) {
            config.owners
        } else {
            0
        };
        assert_eq!(self.joined, workers);
        println!(
            "RAM_CASE {{\"schema\":1,\"domain\":\"{}\",\"case\":\"{}\",\"transport\":\"{}\",\"owners\":{},\"workers\":{},\"stack_request_per_worker\":{},\"stack_request_total\":{},\"queues\":{},\"slots_per_queue\":{},\"event_bytes\":64,\"payload_capacity_bytes\":{},\"gate_static_bytes\":{},\"recorder_stack_bytes\":{},\"endpoint_vector_requested_bytes\":{},\"join_handle_vector_requested_bytes\":{},\"rust_scope\":\"requested-allocation-bytes\",\"native_heap_available\":{},\"native_stack_available\":{},\"workers_joined\":{},\"payload_valid\":true,\"valid\":true,\"released_rust_bytes_delta\":{},\"released_rust_blocks_delta\":{}}}",
            platform::DOMAIN, config.case.name(), transport, config.owners, workers,
            config.stack, workers * config.stack, queues, capacity,
            queues * capacity * size_of::<Event>(), size_of::<(Mutex<GateState>, Condvar)>(),
            size_of::<Self>(), self.endpoint_wiring_bytes, self.join_handle_storage_bytes,
            platform::NATIVE, platform::NATIVE, self.joined,
            last.rust.live as i64 - first.rust.live as i64,
            last.rust.blocks as i64 - first.rust.blocks as i64,
        );
        for sample in &self.samples[..self.length] {
            println!(
                "RAM_SAMPLE {{\"phase\":\"{}\",\"rust_live\":{},\"rust_blocks\":{},\"rust_peak_process\":{},\"alloc\":{},\"zeroed\":{},\"realloc\":{},\"dealloc\":{},\"native_heap_used\":{},\"native_heap_free\":{},\"native_heap_arena\":{},\"main_stack_usable\":{},\"main_stack_observed\":{}}}",
                sample.phase, sample.rust.live, sample.rust.blocks, sample.rust.peak,
                sample.rust.alloc, sample.rust.zeroed, sample.rust.realloc, sample.rust.dealloc,
                optional(sample.heap.used), optional(sample.heap.free), optional(sample.heap.arena),
                optional(sample.main_stack.usable), optional(sample.main_stack.observed),
            );
        }
        for (index, worker) in self.workers[..self.joined].iter().enumerate() {
            println!(
                "RAM_WORKER {{\"index\":{},\"received\":{},\"timeouts\":{},\"stack_usable\":{},\"stack_observed\":{}}}",
                index, worker.received, worker.timeouts,
                optional(worker.stack.usable), optional(worker.stack.observed),
            );
        }
        // Observe the main stack after the substantial reporting path too,
        // without contaminating the allocation/heap measurement windows.
        let reporting_stack = platform::stack();
        println!(
            "RAM_REPORT_STACK {{\"stack_usable\":{},\"stack_observed\":{}}}",
            optional(reporting_stack.usable),
            optional(reporting_stack.observed),
        );
        println!("RAM_END valid=true");
    }
}

struct Optional(usize);
impl std::fmt::Display for Optional {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if platform::NATIVE {
            write!(formatter, "{}", self.0)
        } else {
            formatter.write_str("null")
        }
    }
}
fn optional(value: usize) -> Optional {
    Optional(value)
}

pub fn baseline() {
    let config = Config {
        case: Case::Baseline,
        owners: 0,
        stack: 0,
    };
    let mut record = Record::begin();
    record.sample("baseline");
    record.sample("released");
    record.emit(&config, "none", 0, 0);
}

pub fn threads_from_args() {
    let config = arguments(true);
    let mut record = Record::begin();
    record.sample("baseline");
    let mut handles = Vec::with_capacity(config.owners);
    record.join_handle_storage_bytes = handles.capacity() * size_of::<JoinHandle<WorkerResult>>();
    record.sample("handle-storage");
    for _ in 0..config.owners {
        handles.push(
            thread::Builder::new()
                .stack_size(config.stack)
                .spawn(|| {
                    checkpoint(0);
                    WorkerResult {
                        stack: platform::stack(),
                        ..WorkerResult::default()
                    }
                })
                .expect("thread creation failed"),
        );
    }
    wait_parked(0, config.owners);
    record.sample("workers-parked");
    release(0);
    record.join(handles, 0, 0);
    record.sample("released");
    record.emit(&config, "none", 0, 0);
}

struct Bank<T: Transport> {
    senders: Vec<T::Sender>,
    receivers: Vec<T::Receiver>,
}

fn banks<T: Transport>(owners: usize, queues: usize, capacity: usize) -> Vec<Bank<T>> {
    let mut banks = Vec::with_capacity(owners);
    for _ in 0..owners {
        let mut senders = Vec::with_capacity(queues);
        let mut receivers = Vec::with_capacity(queues);
        for _ in 0..queues {
            let (sender, receiver) = T::bounded(capacity);
            senders.push(sender);
            receivers.push(receiver);
        }
        banks.push(Bank { senders, receivers });
    }
    banks
}

fn endpoint_wiring_bytes<T: Transport>(banks: &Vec<Bank<T>>) -> usize {
    banks.capacity() * size_of::<Bank<T>>()
        + banks
            .iter()
            .map(|bank| {
                bank.senders.capacity() * size_of::<T::Sender>()
                    + bank.receivers.capacity() * size_of::<T::Receiver>()
            })
            .sum::<usize>()
}

fn fill<T: Transport>(banks: &[Bank<T>], queues: usize) {
    for (owner, bank) in banks.iter().enumerate() {
        for lane in 0..LANES {
            let sender = &bank.senders[if queues == 1 { 0 } else { lane }];
            for sequence in 0..SLOTS {
                assert_eq!(T::send(sender, Event::new(owner, lane, sequence)), Ok(()));
            }
        }
        for (lane, sender) in bank.senders.iter().enumerate() {
            let overflow = Event::new(owner, lane, SLOTS);
            assert_eq!(
                T::send(sender, overflow),
                Err(overflow),
                "overflow was not returned intact"
            );
        }
    }
}

fn queue_control<T: Transport>(config: &Config, record: &mut Record) {
    // Both queue-only arms have the historical 3 x 8 capacity topology.
    let mut banks = banks::<T>(config.owners, LANES, SLOTS);
    record.endpoint_wiring_bytes = endpoint_wiring_bytes(&banks);
    record.sample("empty");
    fill::<T>(&banks, LANES);
    record.sample("full");
    for (owner, bank) in banks.iter().enumerate() {
        let mut next = [0; LANES];
        for receiver in &bank.receivers {
            for _ in 0..SLOTS {
                T::try_receive(receiver)
                    .expect("missing admitted event")
                    .verify(owner, &mut next);
            }
            assert_eq!(T::try_receive(receiver), Err(ReceiveError::Empty));
        }
        assert_eq!(next, [SLOTS; LANES]);
    }
    record.sample("drained");
    for bank in &mut banks {
        bank.senders.clear();
        for receiver in &bank.receivers {
            assert_eq!(T::try_receive(receiver), Err(ReceiveError::Closed));
        }
    }
    drop(banks);
    record.sample("released");
    // No threads or blocking waiter caches can excuse a queue-only leak.
    record.samples[record.length - 1]
        .rust
        .reclaimed_since(record.samples[0].rust)
        .unwrap();
}

fn service<T: Transport>(owner: usize, receivers: Vec<T::Receiver>) -> WorkerResult {
    checkpoint(0);
    let mut next = [0; LANES];
    for _ in 0..LANES * SLOTS {
        T::wait(&receivers, CONTROL_TIMEOUT)
            .expect("missing admitted event")
            .verify(owner, &mut next);
    }
    assert_eq!(next, [SLOTS; LANES]);
    for receiver in &receivers {
        assert_eq!(T::try_receive(receiver), Err(ReceiveError::Empty));
    }
    checkpoint(1);
    assert_eq!(T::wait(&receivers, WAIT), Err(ReceiveError::Timeout));
    checkpoint(2);
    for _ in 0..REPEATED_WAITS {
        assert_eq!(T::wait(&receivers, WAIT), Err(ReceiveError::Timeout));
    }
    checkpoint(3);
    assert_eq!(T::wait(&receivers, WAIT), Err(ReceiveError::Closed));
    drop(receivers);
    WorkerResult {
        received: LANES * SLOTS,
        timeouts: 1 + REPEATED_WAITS,
        stack: platform::stack(),
    }
}

fn service_control<T: Transport>(config: &Config, record: &mut Record) {
    let queues = T::SERVICE_QUEUES;
    let capacity = LANES * SLOTS / queues;
    let mut banks = banks::<T>(config.owners, queues, capacity);
    record.endpoint_wiring_bytes = endpoint_wiring_bytes(&banks);
    record.sample("empty");
    let mut handles = Vec::with_capacity(config.owners);
    record.join_handle_storage_bytes = handles.capacity() * size_of::<JoinHandle<WorkerResult>>();
    record.sample("handle-storage");
    for (owner, bank) in banks.iter_mut().enumerate() {
        let receivers = std::mem::take(&mut bank.receivers);
        handles.push(
            thread::Builder::new()
                .stack_size(config.stack)
                .spawn(move || service::<T>(owner, receivers))
                .expect("thread creation failed"),
        );
    }
    wait_parked(0, config.owners);
    record.sample("workers-parked");
    fill::<T>(&banks, queues);
    record.sample("full");
    release(0);
    wait_parked(1, config.owners);
    record.sample("drained");
    release(1);
    wait_parked(2, config.owners);
    record.sample("first-wait-complete");
    release(2);
    wait_parked(3, config.owners);
    record.sample("repeat-wait-complete");
    for bank in &mut banks {
        bank.senders.clear();
    }
    release(3);
    record.join(handles, LANES * SLOTS, REPEATED_WAITS + 1);
    record.sample("joined");
    drop(banks);
    record.sample("released");
}

pub fn channels_from_args<T: Transport>() {
    let config = arguments(false);
    let mut record = Record::begin();
    record.sample("baseline");
    let (queues, capacity) = if config.case == Case::Queues {
        queue_control::<T>(&config, &mut record);
        (LANES, SLOTS)
    } else {
        service_control::<T>(&config, &mut record);
        (T::SERVICE_QUEUES, LANES * SLOTS / T::SERVICE_QUEUES)
    };
    record.emit(&config, T::NAME, config.owners * queues, capacity);
}

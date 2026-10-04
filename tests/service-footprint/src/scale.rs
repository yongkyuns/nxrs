//! Matched C/Rust transport and packet-service qualification on ESP32-S3.
//! This is a finite, checked workload, not a production event runtime.
#![cfg_attr(
    all(feature = "ffi-scale-entry", target_os = "nuttx", not(test)),
    no_main
)]

#[cfg(all(feature = "mq-backend", target_os = "nuttx"))]
#[path = "mq_backend.rs"]
mod channel;
#[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
use channel::TrySendError;
use channel::{bounded, Receiver, Select, Sender};
#[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
use crossbeam_channel as channel;
#[cfg(feature = "native-scale-worker")]
mod native_thread;
#[cfg(all(feature = "ffi-scale-entry", any(target_os = "nuttx", test)))]
use std::ffi::{c_char, c_int, CStr};
use std::io::{self, Write};
#[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
use std::mem::MaybeUninit;
#[cfg(not(all(feature = "ffi-scale-entry", target_os = "nuttx", not(test))))]
use std::process::ExitCode;
#[cfg(feature = "pad-bss")]
use std::sync::atomic::AtomicU8;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant};
#[cfg(feature = "packet-service")]
mod packet_service;
#[cfg(feature = "packet-service")]
mod payload_kernels;

#[cfg(feature = "synchronized-scale")]
unsafe extern "C" {
    fn nxrs_cq_gate_init(count: u32) -> i32;
    fn nxrs_cq_gate_arrive() -> i32;
    fn nxrs_cq_gate_release() -> i32;
    fn nxrs_cq_gate_done();
    fn nxrs_cq_gate_elapsed() -> u32;
    fn nxrs_cq_gate_heap_ready() -> u32;
    fn nxrs_cq_gate_destroy() -> i32;
}

fn traffic_ready() -> Result<(), &'static str> {
    #[cfg(feature = "synchronized-scale")]
    // SAFETY: the command initialized the common gate before spawning peers.
    if unsafe { nxrs_cq_gate_arrive() } != 0 {
        return Err("traffic gate failed");
    }
    Ok(())
}

#[cfg(feature = "native-scale-worker")]
type ScaleJoinHandle<T> = native_thread::JoinHandle<T>;
#[cfg(not(feature = "native-scale-worker"))]
type ScaleJoinHandle<T> = thread::JoinHandle<T>;

fn spawn_scale<F, T, N>(name: N, stack: usize, work: F) -> Result<ScaleJoinHandle<T>, ()>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
    N: FnOnce() -> String,
{
    #[cfg(feature = "native-scale-worker")]
    {
        let _ = name;
        native_thread::spawn(stack, work)
    }
    #[cfg(not(feature = "native-scale-worker"))]
    {
        thread::Builder::new()
            .name(name())
            .stack_size(stack)
            .spawn(work)
            .map_err(|_| ())
    }
}

fn join_scale<T>(handle: ScaleJoinHandle<T>) -> Result<T, ()> {
    handle.join().map_err(|_| ())
}

const PRODUCERS: usize = 4;
const MEDIUM_WORKERS: usize = 12;
const MEDIUM_SEQUENCES: u16 = 32;
#[cfg(feature = "compact-topology")]
const LARGE_WORKERS: usize = 5;
#[cfg(not(feature = "compact-topology"))]
const LARGE_WORKERS: usize = 15;
#[cfg(feature = "compact-topology")]
const LARGE_LANES_PER_WORKER: usize = 11;
#[cfg(not(feature = "compact-topology"))]
const LARGE_LANES_PER_WORKER: usize = 3;
#[cfg(feature = "compact-topology")]
const LARGE_SEQUENCES: u16 = 26;
#[cfg(not(feature = "compact-topology"))]
const LARGE_SEQUENCES: u16 = 32;
const SMALL_SEQUENCES: u16 = 64;
const SAMPLE_EVERY: u16 = 8;
const WAKE_WARMUP: usize = 4;
const WAKE_TRIALS: usize = 64;
#[cfg(target_os = "nuttx")]
const STACK_BYTES: usize = 4_096;
#[cfg(not(target_os = "nuttx"))]
const STACK_BYTES: usize = 65_536;
#[cfg(target_os = "nuttx")]
const COLLECTOR_STACK_BYTES: usize = 6_144;
#[cfg(not(target_os = "nuttx"))]
const COLLECTOR_STACK_BYTES: usize = 65_536;
const CYCLES_PER_US: u32 = 240;
#[cfg(all(feature = "packet-service", feature = "legacy-payload-copy"))]
compile_error!("packet-service has no legacy payload-copy mode");
#[cfg(all(feature = "legacy-payload-copy", feature = "in-place-payload"))]
compile_error!("legacy-payload-copy and in-place-payload are mutually exclusive");
static THREADS_READY: AtomicUsize = AtomicUsize::new(0);
static THREADS_RELEASE: AtomicBool = AtomicBool::new(false);
#[cfg(all(test, feature = "synchronized-scale"))]
static TEST_GATE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

// The stock Crossbeam image retained 4,288 B of atomic fallback locks and
// 48 B of alignment fill. This probe deliberately places the same amount of
// mutable zero-initialized storage back into a no-tick image. The actual ELF
// sections still have to be checked before claiming layout equivalence.
#[cfg(feature = "pad-bss")]
#[used]
static BSS_PAD: [AtomicU8; 4_336] = [const { AtomicU8::new(0) }; 4_336];

#[cfg(target_os = "nuttx")]
unsafe extern "C" {
    fn nxrs_cq_cycles() -> u32;
    fn nxrs_cq_stack_hwm() -> u32;
    fn nxrs_cq_heap_used() -> u32;
    #[cfg(feature = "c-checksum")]
    fn nxrs_cq_checksum(lane: u8, producer: u8, sequence: u16, payload: *const u8) -> u32;
}

fn cycles() -> u32 {
    #[cfg(target_os = "nuttx")]
    {
        // SAFETY: the target C helper only reads the configured, single-core
        // Xtensa CCOUNT register and retains no Rust pointer or resource.
        unsafe { nxrs_cq_cycles() }
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        use std::sync::OnceLock;
        static EPOCH: OnceLock<Instant> = OnceLock::new();
        let elapsed = EPOCH.get_or_init(Instant::now).elapsed().as_nanos();
        (elapsed.saturating_mul(u128::from(CYCLES_PER_US)) / 1_000) as u32
    }
}

fn micros(cycle_delta: u32) -> u32 {
    cycle_delta.div_ceil(CYCLES_PER_US)
}

fn stack_hwm() -> u32 {
    #[cfg(target_os = "nuttx")]
    {
        // SAFETY: the helper reads coloration on the current NuttX task stack.
        unsafe { nxrs_cq_stack_hwm() }
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        0
    }
}

fn heap_used() -> u32 {
    #[cfg(target_os = "nuttx")]
    {
        // SAFETY: the helper returns a snapshot of NuttX's OS heap counter.
        unsafe { nxrs_cq_heap_used() }
    }
    #[cfg(not(target_os = "nuttx"))]
    {
        0
    }
}

#[cfg_attr(all(feature = "mq-backend", target_os = "nuttx"), repr(C))]
#[derive(Clone, Copy)]
struct Event {
    lane: u8,
    producer: u8,
    sequence: u16,
    sent_cycles: u32,
    checksum: u32,
    payload: [u8; 236],
}

impl Event {
    #[cfg(all(not(feature = "wire-only"), not(feature = "legacy-payload-copy")))]
    const fn empty() -> Self {
        Self {
            lane: 0,
            producer: 0,
            sequence: 0,
            sent_cycles: 0,
            checksum: 0,
            payload: [0; 236],
        }
    }

    /// Overwrite an already initialized event. Producers reuse their private
    /// buffer only after send has copied it; no uninitialized memory is exposed.
    #[cfg(all(not(feature = "wire-only"), not(feature = "legacy-payload-copy")))]
    fn reset(&mut self, lane: usize, producer: usize, sequence: u16) {
        self.lane = lane as u8;
        self.producer = producer as u8;
        self.sequence = sequence;
        #[cfg(not(feature = "packet-service"))]
        {
            fill_payload(&mut self.payload, self.lane, self.producer, sequence);
            self.checksum = checksum(self.lane, self.producer, sequence, &self.payload);
        }
        #[cfg(feature = "packet-service")]
        {
            packet_service::build(&mut self.payload, self.lane, self.producer, sequence);
            self.checksum = packet_service::token(self.lane, self.producer, sequence) ^ 0xa5a5a5a5;
        }
        // Match C: receive latency starts after application payload work.
        self.sent_cycles = if sequence.is_multiple_of(SAMPLE_EVERY) {
            cycles()
        } else {
            0
        };
    }

    #[cfg(any(
        all(test, not(feature = "packet-service")),
        feature = "wire-only",
        feature = "legacy-payload-copy"
    ))]
    fn new(lane: usize, producer: usize, sequence: u16) -> Self {
        #[cfg(all(not(feature = "legacy-payload-copy"), not(feature = "wire-only")))]
        {
            let mut event = Self::empty();
            event.reset(lane, producer, sequence);
            event
        }
        #[cfg(any(feature = "legacy-payload-copy", feature = "wire-only"))]
        {
            let payload = payload(lane as u8, producer as u8, sequence);
            #[cfg(feature = "wire-only")]
            let checksum = wire_checksum(lane as u8, producer as u8, sequence);
            #[cfg(not(feature = "wire-only"))]
            let checksum = checksum(lane as u8, producer as u8, sequence, &payload);
            let sent_cycles = if sequence.is_multiple_of(SAMPLE_EVERY) {
                cycles()
            } else {
                0
            };
            Self {
                lane: lane as u8,
                producer: producer as u8,
                sequence,
                sent_cycles,
                checksum,
                payload,
            }
        }
    }

    fn valid(&self) -> bool {
        #[cfg(feature = "wire-only")]
        {
            self.payload[0] == 0xa5
                && self.payload[self.payload.len() - 1] == 0xa5
                && self.checksum == wire_checksum(self.lane, self.producer, self.sequence)
        }
        #[cfg(all(not(feature = "wire-only"), not(feature = "packet-service")))]
        {
            self.checksum == checksum(self.lane, self.producer, self.sequence, &self.payload)
        }
        #[cfg(all(not(feature = "wire-only"), feature = "packet-service"))]
        {
            self.checksum
                == packet_service::token(self.lane, self.producer, self.sequence) ^ 0xa5a5a5a5
        }
    }
}

#[cfg(feature = "wire-only")]
fn payload(_lane: u8, _producer: u8, _sequence: u16) -> [u8; 236] {
    [0xa5; 236]
}

#[cfg(all(not(feature = "wire-only"), feature = "legacy-payload-copy"))]
fn payload(lane: u8, producer: u8, sequence: u16) -> [u8; 236] {
    let mut payload = [0u8; 236];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = lane
            .wrapping_mul(17)
            .wrapping_add(producer)
            .wrapping_add(sequence as u8)
            ^ index as u8;
    }
    payload
}

#[cfg(all(
    not(feature = "wire-only"),
    not(feature = "legacy-payload-copy"),
    not(feature = "packet-service")
))]
fn fill_payload(payload: &mut [u8; 236], lane: u8, producer: u8, sequence: u16) {
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = lane
            .wrapping_mul(17)
            .wrapping_add(producer)
            .wrapping_add(sequence as u8)
            ^ index as u8;
    }
}

#[cfg(feature = "wire-only")]
fn wire_checksum(lane: u8, producer: u8, sequence: u16) -> u32 {
    (u32::from(lane) << 24 | u32::from(producer) << 16 | u32::from(sequence)) ^ 0xa5a5_a5a5
}

#[cfg(all(not(feature = "wire-only"), not(feature = "packet-service")))]
fn checksum(lane: u8, producer: u8, sequence: u16, payload: &[u8; 236]) -> u32 {
    #[cfg(all(feature = "c-checksum", target_os = "nuttx"))]
    {
        // SAFETY: the fixed-size payload remains live for the synchronous C
        // checksum, which reads exactly 236 bytes and retains no pointer.
        return unsafe { nxrs_cq_checksum(lane, producer, sequence, payload.as_ptr()) };
    }
    #[cfg(not(all(feature = "c-checksum", target_os = "nuttx")))]
    {
        let seed = (u32::from(lane) << 24) | (u32::from(producer) << 16) | u32::from(sequence);
        payload.iter().fold(seed, |sum, byte| {
            sum.rotate_left(3).wrapping_add(u32::from(*byte))
        })
    }
}

#[cfg_attr(all(feature = "mq-backend", target_os = "nuttx"), repr(C))]
#[derive(Clone, Copy)]
struct Ack {
    lane: u8,
    producer: u8,
    sequence: u16,
    checksum: u32,
    sent_cycles: u32,
    received_cycles: u32,
    #[cfg(feature = "packet-service")]
    filtered: [i32; 3],
}

#[derive(Clone, Copy)]
struct Config {
    label: &'static str,
    workers: usize,
    lanes_per_worker: usize,
    sequences: u16,
}

impl Config {
    fn lanes(self) -> usize {
        self.workers * self.lanes_per_worker
    }

    fn events(self) -> usize {
        self.lanes() * PRODUCERS * usize::from(self.sequences)
    }

    fn samples(self) -> usize {
        self.lanes() * PRODUCERS * usize::from(self.sequences.div_ceil(SAMPLE_EVERY))
    }

    fn queues(self) -> usize {
        let scale = if cfg!(feature = "multiplexed") {
            self.workers * 2
        } else {
            self.lanes() + self.workers
        };
        scale
    }

    fn logical_streams(self) -> usize {
        self.lanes() + self.workers
    }

    fn name(self) -> &'static str {
        self.label
    }
}

struct ProducerReport {
    sent: usize,
    full: usize,
    stack_hwm: u32,
    #[cfg(feature = "profile-phases")]
    phases: PhaseTimes,
}

#[cfg(feature = "profile-phases")]
#[derive(Default)]
struct PhaseTimes {
    producer_build: u64,
    producer_send: u64,
    worker_poll: u64,
    worker_receive: u64,
    worker_validate: u64,
    worker_send: u64,
    collector_poll: u64,
    collector_receive: u64,
    collector_validate: u64,
}

fn producer(
    id: usize,
    senders: Vec<Sender<Event>>,
    sequences: u16,
) -> Result<ProducerReport, &'static str> {
    #[cfg(feature = "profile-phases")]
    let mut phases = PhaseTimes::default();
    #[cfg(all(feature = "mq-backend", target_os = "nuttx"))]
    let full = 0;
    #[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
    let mut full = 0;
    #[cfg(all(not(feature = "wire-only"), not(feature = "legacy-payload-copy")))]
    let mut event = Event::empty();
    traffic_ready()?;
    for sequence in 0..sequences {
        for (lane, sender) in senders.iter().enumerate() {
            #[cfg(feature = "profile-phases")]
            let phase_started = cycles();
            #[cfg(any(feature = "wire-only", feature = "legacy-payload-copy"))]
            let event = Event::new(lane, id, sequence);
            #[cfg(all(not(feature = "wire-only"), not(feature = "legacy-payload-copy")))]
            event.reset(lane, id, sequence);
            #[cfg(feature = "profile-phases")]
            {
                phases.producer_build += u64::from(cycles().wrapping_sub(phase_started));
            }
            #[cfg(feature = "profile-phases")]
            let phase_started = cycles();
            #[cfg(all(
                target_os = "nuttx",
                feature = "mq-backend",
                any(
                    feature = "borrowed-mq-io",
                    all(not(feature = "wire-only"), not(feature = "legacy-payload-copy"))
                )
            ))]
            sender
                .send_ref(&event)
                .map_err(|_| "input mq_send failed")?;
            #[cfg(all(
                feature = "mq-backend",
                not(feature = "borrowed-mq-io"),
                any(feature = "wire-only", feature = "legacy-payload-copy"),
                target_os = "nuttx"
            ))]
            sender.send(event).map_err(|_| "input mq_send failed")?;
            #[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
            match sender.try_send(event) {
                Ok(()) => {}
                Err(TrySendError::Full(event)) => {
                    full += 1;
                    sender
                        .send(event)
                        .map_err(|_| "input channel disconnected")?;
                }
                Err(TrySendError::Disconnected(_)) => return Err("input channel disconnected"),
            }
            #[cfg(feature = "profile-phases")]
            {
                phases.producer_send += u64::from(cycles().wrapping_sub(phase_started));
            }
        }
    }
    Ok(ProducerReport {
        sent: senders.len() * usize::from(sequences),
        full,
        stack_hwm: stack_hwm(),
        #[cfg(feature = "profile-phases")]
        phases,
    })
}

struct WorkerReport {
    handled: usize,
    stack_hwm: u32,
    #[cfg(feature = "profile-phases")]
    phases: PhaseTimes,
}

fn worker(
    id: usize,
    inputs: Vec<Receiver<Event>>,
    output: Sender<Ack>,
    lanes_per_worker: usize,
    sequences: u16,
) -> Result<WorkerReport, &'static str> {
    #[cfg(feature = "profile-phases")]
    let mut phases = PhaseTimes::default();
    let mut selection = Select::new();
    for input in &inputs {
        selection.recv(input);
    }
    let mut next = vec![[0u16; PRODUCERS]; lanes_per_worker];
    #[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
    let mut event_buffer = MaybeUninit::<Event>::uninit();
    let mut handled = 0;
    let mut open = inputs.len();
    #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
    let mut filter_states = [[[0i32; 3]; PRODUCERS]; LARGE_LANES_PER_WORKER];
    traffic_ready()?;
    while handled < lanes_per_worker * PRODUCERS * usize::from(sequences) {
        if open == 0 {
            return Err("all inputs closed before expected count");
        }
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        let selected = selection.select();
        #[cfg(feature = "profile-phases")]
        {
            phases.worker_poll += u64::from(cycles().wrapping_sub(phase_started));
        }
        let which = selected.index();
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        #[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
        // SAFETY: Event is repr(C) with only integer/byte fields, and the
        // selected queue was created with exactly Event-sized messages.
        let incoming = unsafe { selected.recv_into(&inputs[which], &mut event_buffer) };
        #[cfg(not(all(feature = "borrowed-mq-io", target_os = "nuttx")))]
        let incoming = selected.recv(&inputs[which]);
        let event = match incoming {
            Ok(event) => event,
            Err(_) => {
                #[cfg(feature = "multiplexed")]
                let incomplete = next.iter().flatten().any(|&count| count != sequences);
                #[cfg(not(feature = "multiplexed"))]
                let incomplete = next[which].iter().any(|&count| count != sequences);
                if incomplete {
                    return Err("input channel disconnected early");
                }
                selection.remove(which);
                open -= 1;
                continue;
            }
        };
        #[cfg(feature = "profile-phases")]
        {
            phases.worker_receive += u64::from(cycles().wrapping_sub(phase_started));
        }
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        let producer = usize::from(event.producer);
        let lane = usize::from(event.lane);
        #[cfg(feature = "multiplexed")]
        let local = lane
            .checked_sub(id * lanes_per_worker)
            .filter(|&index| index < lanes_per_worker)
            .ok_or("input lane assigned to wrong worker")?;
        #[cfg(not(feature = "multiplexed"))]
        let local = which;
        if lane != id * lanes_per_worker + local
            || producer >= PRODUCERS
            || event.sequence != next[local][producer]
            || !event.valid()
        {
            return Err("input payload or per-sender order invalid");
        }
        #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
        let filtered = packet_service::process(
            &event.payload,
            packet_service::token(event.lane, event.producer, event.sequence),
            &mut filter_states[local][producer],
        )
        .map_err(|_| "packet processing failed")?;
        next[local][producer] += 1;
        let received_cycles = if event.sequence.is_multiple_of(SAMPLE_EVERY) {
            cycles()
        } else {
            0
        };
        #[cfg(feature = "profile-phases")]
        {
            phases.worker_validate += u64::from(cycles().wrapping_sub(phase_started));
        }
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        let ack = Ack {
            lane: event.lane,
            producer: event.producer,
            sequence: event.sequence,
            checksum: event.checksum,
            sent_cycles: event.sent_cycles,
            received_cycles,
            #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
            filtered,
            #[cfg(all(feature = "packet-service", feature = "wire-only"))]
            filtered: [0; 3],
        };
        #[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
        output
            .send_ref(&ack)
            .map_err(|_| "outbound channel disconnected")?;
        #[cfg(not(all(feature = "borrowed-mq-io", target_os = "nuttx")))]
        output
            .send(ack)
            .map_err(|_| "outbound channel disconnected")?;
        #[cfg(feature = "profile-phases")]
        {
            phases.worker_send += u64::from(cycles().wrapping_sub(phase_started));
        }
        handled += 1;
    }
    if next.iter().flatten().any(|&count| count != sequences) {
        return Err("worker missed an input event");
    }
    Ok(WorkerReport {
        handled,
        stack_hwm: stack_hwm(),
        #[cfg(feature = "profile-phases")]
        phases,
    })
}

struct CollectorReport {
    received: usize,
    digest: u32,
    inbound_us: Vec<u32>,
    outbound_us: Vec<u32>,
    stack_hwm: u32,
    #[cfg(feature = "profile-phases")]
    phases: PhaseTimes,
}

fn collector(outputs: Vec<Receiver<Ack>>, config: Config) -> Result<CollectorReport, &'static str> {
    #[cfg(feature = "profile-phases")]
    let mut phases = PhaseTimes::default();
    let mut next = vec![0u16; config.lanes() * PRODUCERS];
    #[cfg(all(
        not(feature = "wire-only"),
        not(feature = "legacy-payload-copy"),
        not(feature = "packet-service")
    ))]
    let mut expected_payload = [0u8; 236];
    #[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
    let mut ack_buffer = MaybeUninit::<Ack>::uninit();
    let mut selection = Select::new();
    for output in &outputs {
        selection.recv(output);
    }
    let samples = config.samples();
    let mut inbound_us = Vec::with_capacity(samples);
    let mut outbound_us = Vec::with_capacity(samples);
    let mut received = 0;
    let mut digest = 0u32;
    let mut open = outputs.len();
    #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
    let mut filter_states = [[0i32; 3]; LARGE_WORKERS * LARGE_LANES_PER_WORKER * PRODUCERS];
    #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
    let mut packet = [0u8; 236];
    traffic_ready()?;
    while received < config.events() {
        if open == 0 {
            return Err("all outputs closed before expected count");
        }
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        let selected = selection.select();
        #[cfg(feature = "profile-phases")]
        {
            phases.collector_poll += u64::from(cycles().wrapping_sub(phase_started));
        }
        let index = selected.index();
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        #[cfg(all(feature = "borrowed-mq-io", target_os = "nuttx"))]
        // SAFETY: Ack is repr(C) with only integer fields, and the selected
        // queue was created with exactly Ack-sized messages.
        let incoming = unsafe { selected.recv_into(&outputs[index], &mut ack_buffer) };
        #[cfg(not(all(feature = "borrowed-mq-io", target_os = "nuttx")))]
        let incoming = selected.recv(&outputs[index]);
        let ack = match incoming {
            Ok(ack) => ack,
            Err(_) => {
                let first_lane = index * config.lanes_per_worker;
                let last_lane = first_lane + config.lanes_per_worker;
                if (first_lane..last_lane).any(|lane| {
                    (0..PRODUCERS)
                        .any(|producer| next[lane * PRODUCERS + producer] != config.sequences)
                }) {
                    return Err("outbound channel disconnected early");
                }
                selection.remove(index);
                open -= 1;
                continue;
            }
        };
        #[cfg(feature = "profile-phases")]
        {
            phases.collector_receive += u64::from(cycles().wrapping_sub(phase_started));
        }
        #[cfg(feature = "profile-phases")]
        let phase_started = cycles();
        let collected_cycles = if ack.sequence.is_multiple_of(SAMPLE_EVERY) {
            cycles()
        } else {
            0
        };
        let lane = usize::from(ack.lane);
        let producer = usize::from(ack.producer);
        if lane >= config.lanes()
            || producer >= PRODUCERS
            || lane / config.lanes_per_worker != index
            || ack.sequence != next[lane * PRODUCERS + producer]
        {
            return Err("outbound event missing, duplicated, or out of order");
        }
        #[cfg(feature = "wire-only")]
        let expected = wire_checksum(ack.lane, ack.producer, ack.sequence);
        #[cfg(all(feature = "packet-service", feature = "wire-only"))]
        if ack.filtered != [0; 3] {
            return Err("wire reply contains filter state");
        }
        #[cfg(all(not(feature = "wire-only"), feature = "legacy-payload-copy"))]
        let expected = checksum(
            ack.lane,
            ack.producer,
            ack.sequence,
            &payload(ack.lane, ack.producer, ack.sequence),
        );
        #[cfg(all(
            not(feature = "legacy-payload-copy"),
            not(feature = "wire-only"),
            not(feature = "packet-service")
        ))]
        let expected = {
            fill_payload(&mut expected_payload, ack.lane, ack.producer, ack.sequence);
            checksum(ack.lane, ack.producer, ack.sequence, &expected_payload)
        };
        #[cfg(all(feature = "packet-service", not(feature = "wire-only")))]
        let expected = {
            let id = packet_service::token(ack.lane, ack.producer, ack.sequence);
            if ack.checksum != id ^ 0xa5a5a5a5 {
                return Err("outbound checksum mismatch");
            }
            packet_service::build(&mut packet, ack.lane, ack.producer, ack.sequence);
            let filtered = packet_service::process(
                &packet,
                id,
                &mut filter_states[lane * PRODUCERS + producer],
            )
            .map_err(|_| "collector packet processing failed")?;
            if filtered != ack.filtered {
                return Err("filtered reply mismatch");
            }
            id ^ 0xa5a5a5a5
        };
        if ack.checksum != expected {
            return Err("outbound checksum mismatch");
        }
        next[lane * PRODUCERS + producer] += 1;
        received += 1;
        digest = digest.wrapping_add(ack.checksum);
        if ack.sequence.is_multiple_of(SAMPLE_EVERY) {
            inbound_us.push(micros(ack.received_cycles.wrapping_sub(ack.sent_cycles)));
            outbound_us.push(micros(collected_cycles.wrapping_sub(ack.received_cycles)));
        }
        #[cfg(feature = "profile-phases")]
        {
            phases.collector_validate += u64::from(cycles().wrapping_sub(phase_started));
        }
    }
    if received != config.events() || next.iter().any(|&count| count != config.sequences) {
        return Err("collector missed an outbound event");
    }
    if inbound_us.len() != config.samples() || outbound_us.len() != config.samples() {
        return Err("collector sample count mismatch");
    }
    #[cfg(feature = "synchronized-scale")]
    // SAFETY: only the collector records the validated completion boundary.
    unsafe {
        nxrs_cq_gate_done();
    }
    Ok(CollectorReport {
        received,
        digest,
        inbound_us,
        outbound_us,
        stack_hwm: stack_hwm(),
        #[cfg(feature = "profile-phases")]
        phases,
    })
}

fn percentile(samples: &mut [u32], percent: usize) -> u32 {
    samples.sort_unstable();
    let rank = (samples.len() * percent).div_ceil(100).saturating_sub(1);
    samples[rank]
}

struct Report {
    config: Config,
    full: usize,
    digest: u32,
    elapsed_us: u64,
    #[cfg(feature = "synchronized-scale")]
    traffic_cycles: u32,
    inbound_p50_us: u32,
    inbound_p99_us: u32,
    inbound_max_us: u32,
    outbound_p99_us: u32,
    producer_stack_max: u32,
    worker_stack_max: u32,
    collector_stack: u32,
    heap_before: u32,
    heap_queues: u32,
    heap_workers: u32,
    heap_producers: u32,
    heap_after_join: u32,
    #[cfg(feature = "profile-phases")]
    phases: PhaseTimes,
}

fn run(config: Config) -> Result<Report, &'static str> {
    #[cfg(all(test, feature = "synchronized-scale"))]
    let _test_guard = TEST_GATE_LOCK
        .lock()
        .expect("qualification test lock poisoned");
    #[cfg(feature = "pad-bss")]
    {
        BSS_PAD[0].fetch_add(1, Ordering::Relaxed);
        BSS_PAD[BSS_PAD.len() - 1].fetch_add(1, Ordering::Relaxed);
    }

    let heap_before = heap_used();
    #[cfg(feature = "synchronized-scale")]
    // SAFETY: one command owns this gate; all prior runs joined and destroyed it.
    if unsafe { nxrs_cq_gate_init((config.workers + PRODUCERS + 1) as u32) } != 0 {
        return Err("traffic gate initialization failed");
    }
    let mut senders = Vec::with_capacity(config.lanes());
    #[cfg(feature = "multiplexed")]
    let mut receivers = Vec::with_capacity(config.workers);
    #[cfg(not(feature = "multiplexed"))]
    let mut receivers = Vec::with_capacity(config.lanes());
    #[cfg(feature = "multiplexed")]
    for _ in 0..config.workers {
        let (sender, receiver) = bounded::<Event>(config.lanes_per_worker);
        for _ in 0..config.lanes_per_worker {
            senders.push(sender.clone());
        }
        receivers.push(receiver);
    }
    #[cfg(not(feature = "multiplexed"))]
    for _ in 0..config.lanes() {
        let (sender, receiver) = bounded::<Event>(1);
        senders.push(sender);
        receivers.push(receiver);
    }
    let mut output_senders = Vec::with_capacity(config.workers);
    let mut output_receivers = Vec::with_capacity(config.workers);
    for _ in 0..config.workers {
        let (sender, receiver) = bounded::<Ack>(4);
        output_senders.push(sender);
        output_receivers.push(receiver);
    }
    let heap_queues = heap_used();
    let collector = spawn_scale(
        || "cq-scale-collector".to_owned(),
        COLLECTOR_STACK_BYTES,
        move || collector(output_receivers, config),
    )
    .map_err(|_| "collector spawn failed")?;
    let mut workers = Vec::with_capacity(config.workers);
    for (id, output) in output_senders.into_iter().enumerate() {
        #[cfg(feature = "multiplexed")]
        let input_count = 1;
        #[cfg(not(feature = "multiplexed"))]
        let input_count = config.lanes_per_worker;
        let inputs = receivers.drain(..input_count).collect();
        workers.push(
            spawn_scale(
                || format!("cq-scale-worker-{id}"),
                STACK_BYTES,
                move || {
                    worker(
                        id,
                        inputs,
                        output,
                        config.lanes_per_worker,
                        config.sequences,
                    )
                },
            )
            .map_err(|_| "worker spawn failed")?,
        );
    }
    let heap_workers = heap_used();
    let started = Instant::now();
    let mut producers = Vec::with_capacity(PRODUCERS);
    for id in 0..PRODUCERS {
        let endpoints = senders.clone();
        producers.push(
            spawn_scale(
                || format!("cq-scale-producer-{id}"),
                STACK_BYTES,
                move || producer(id, endpoints, config.sequences),
            )
            .map_err(|_| "producer spawn failed")?,
        );
    }
    drop(senders);
    #[cfg(not(feature = "synchronized-scale"))]
    let heap_producers = heap_used();
    #[cfg(feature = "synchronized-scale")]
    // SAFETY: every role arrives only after its per-thread setup is complete.
    if unsafe { nxrs_cq_gate_release() } != 0 {
        return Err("traffic release failed");
    }
    #[cfg(feature = "synchronized-scale")]
    // SAFETY: common gate captured this while every initialized peer was blocked.
    let heap_producers = unsafe { nxrs_cq_gate_heap_ready() };
    let mut full = 0;
    let mut sent = 0;
    let mut producer_stack_max = 0;
    #[cfg(feature = "profile-phases")]
    let mut phases = PhaseTimes::default();
    for producer in producers {
        let report = join_scale(producer).map_err(|_| "producer panicked")??;
        sent += report.sent;
        full += report.full;
        producer_stack_max = producer_stack_max.max(report.stack_hwm);
        #[cfg(feature = "profile-phases")]
        {
            phases.producer_build += report.phases.producer_build;
            phases.producer_send += report.phases.producer_send;
        }
    }
    let mut handled = 0;
    let mut worker_stack_max = 0;
    for worker in workers {
        let report = join_scale(worker).map_err(|_| "worker panicked")??;
        handled += report.handled;
        worker_stack_max = worker_stack_max.max(report.stack_hwm);
        #[cfg(feature = "profile-phases")]
        {
            phases.worker_poll += report.phases.worker_poll;
            phases.worker_receive += report.phases.worker_receive;
            phases.worker_validate += report.phases.worker_validate;
            phases.worker_send += report.phases.worker_send;
        }
    }
    let mut collected = join_scale(collector).map_err(|_| "collector panicked")??;
    #[cfg(feature = "profile-phases")]
    {
        phases.collector_poll += collected.phases.collector_poll;
        phases.collector_receive += collected.phases.collector_receive;
        phases.collector_validate += collected.phases.collector_validate;
    }
    let elapsed = started.elapsed();
    #[cfg(feature = "synchronized-scale")]
    let traffic_cycles = unsafe {
        // SAFETY: all threads joined, so completion is published and no waiters remain.
        let duration = nxrs_cq_gate_elapsed();
        if nxrs_cq_gate_destroy() != 0 || duration == 0 {
            return Err("invalid traffic clock");
        }
        duration
    };
    let heap_after_join = heap_used();
    if sent != config.events() || handled != sent || collected.received != sent {
        return Err("end-to-end event count mismatch");
    }
    if collected.inbound_us.len() != config.samples()
        || collected.outbound_us.len() != collected.inbound_us.len()
    {
        return Err("latency sample count mismatch");
    }
    let inbound_max_us = *collected
        .inbound_us
        .iter()
        .max()
        .ok_or("no latency samples")?;
    let outbound_p99_us = percentile(&mut collected.outbound_us, 99);
    let inbound_p50_us = percentile(&mut collected.inbound_us, 50);
    let inbound_p99_us = percentile(&mut collected.inbound_us, 99);
    Ok(Report {
        config,
        full,
        digest: collected.digest,
        elapsed_us: u64::try_from(elapsed.as_micros()).unwrap_or(u64::MAX),
        #[cfg(feature = "synchronized-scale")]
        traffic_cycles,
        inbound_p50_us,
        inbound_p99_us,
        inbound_max_us,
        outbound_p99_us,
        producer_stack_max,
        worker_stack_max,
        collector_stack: collected.stack_hwm,
        heap_before,
        heap_queues,
        heap_workers,
        heap_producers,
        heap_after_join,
        #[cfg(feature = "profile-phases")]
        phases,
    })
}

fn print_report(report: &Report) {
    #[cfg(feature = "synchronized-scale")]
    println!(
        "CQ_TRANSPORT_PASS language=rust mode={} cycles={} event_bytes={} ack_bytes={}",
        report.config.label,
        report.traffic_cycles,
        std::mem::size_of::<Event>(),
        std::mem::size_of::<Ack>()
    );
    println!(
        "CQ_SCALE_PASS mode={} queues={} logical_streams={} threads={} messages={} full={} elapsed_us={} rx_p50_us={} rx_p99_us={} rx_max_us={} ack_p99_us={} digest={} stack_main={} stack_producer_max={} stack_worker_max={} stack_collector={} heap_before={} heap_queues={} heap_workers={} heap_producers={} heap_after_join={}",
        report.config.name(),
        report.config.queues(),
        report.config.logical_streams(),
        report.config.workers + PRODUCERS + 1,
        report.config.events(),
        report.full,
        report.elapsed_us,
        report.inbound_p50_us,
        report.inbound_p99_us,
        report.inbound_max_us,
        report.outbound_p99_us,
        report.digest,
        stack_hwm(),
        report.producer_stack_max,
        report.worker_stack_max,
        report.collector_stack,
        report.heap_before,
        report.heap_queues,
        report.heap_workers,
        report.heap_producers,
        report.heap_after_join
    );
    #[cfg(feature = "profile-phases")]
    println!(
        "CQ_PROFILE_PASS mode={} producer_build_us={} producer_send_us={} worker_poll_us={} worker_receive_us={} worker_validate_us={} worker_send_us={} collector_poll_us={} collector_receive_us={} collector_validate_us={}",
        report.config.name(),
        report.phases.producer_build / u64::from(CYCLES_PER_US),
        report.phases.producer_send / u64::from(CYCLES_PER_US),
        report.phases.worker_poll / u64::from(CYCLES_PER_US),
        report.phases.worker_receive / u64::from(CYCLES_PER_US),
        report.phases.worker_validate / u64::from(CYCLES_PER_US),
        report.phases.worker_send / u64::from(CYCLES_PER_US),
        report.phases.collector_poll / u64::from(CYCLES_PER_US),
        report.phases.collector_receive / u64::from(CYCLES_PER_US),
        report.phases.collector_validate / u64::from(CYCLES_PER_US),
    );
}

fn idle_thread() -> u32 {
    THREADS_READY.fetch_add(1, Ordering::Release);
    while !THREADS_RELEASE.load(Ordering::Acquire) {
        thread::sleep(Duration::from_millis(1));
    }
    stack_hwm()
}

/// Same thread count and stack reservations as the large case, but no queues.
/// The live snapshot isolates the cost of adding endpoints and traffic.
fn run_thread_baseline() -> Result<(u32, u32, u32), &'static str> {
    THREADS_READY.store(0, Ordering::Relaxed);
    THREADS_RELEASE.store(false, Ordering::Relaxed);
    let heap_before = heap_used();
    let mut handles = Vec::with_capacity(LARGE_WORKERS + PRODUCERS + 1);
    macro_rules! spawn_idle {
        ($name:expr, $stack:expr) => {
            match spawn_scale(|| $name.to_owned(), $stack, idle_thread) {
                Ok(handle) => handles.push(handle),
                Err(_) => {
                    THREADS_RELEASE.store(true, Ordering::Release);
                    for handle in handles {
                        let _ = join_scale(handle);
                    }
                    return Err("thread baseline spawn failed");
                }
            }
        };
    }
    spawn_idle!("cq-scale-collector", COLLECTOR_STACK_BYTES);
    for id in 0..LARGE_WORKERS {
        spawn_idle!(format!("cq-scale-worker-{id}"), STACK_BYTES);
    }
    for id in 0..PRODUCERS {
        spawn_idle!(format!("cq-scale-producer-{id}"), STACK_BYTES);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while THREADS_READY.load(Ordering::Acquire) != handles.len() {
        if Instant::now() >= deadline {
            THREADS_RELEASE.store(true, Ordering::Release);
            for handle in handles {
                let _ = join_scale(handle);
            }
            return Err("thread baseline start timed out");
        }
        thread::sleep(Duration::from_millis(1));
    }
    let heap_live = heap_used();
    THREADS_RELEASE.store(true, Ordering::Release);
    for handle in handles {
        join_scale(handle).map_err(|_| "thread baseline panic")?;
    }
    Ok((heap_before, heap_live, heap_used()))
}

#[cfg_attr(all(feature = "mq-backend", target_os = "nuttx"), repr(C))]
#[derive(Clone, Copy)]
struct WakeProbe {
    sent_cycles: u32,
}

#[cfg_attr(all(feature = "mq-backend", target_os = "nuttx"), repr(C))]
#[derive(Clone, Copy)]
struct WakeAck {
    sent_cycles: u32,
    received_cycles: u32,
}

struct WakeReport {
    sleep_clock_us: u32,
    inbound_p50_us: u32,
    inbound_p99_us: u32,
    inbound_max_us: u32,
    outbound_p99_us: u32,
}

fn run_wake(trials: usize, sleep_ms: u64) -> Result<WakeReport, &'static str> {
    let (tx0, rx0) = bounded::<WakeProbe>(1);
    let (tx1, rx1) = bounded::<WakeProbe>(1);
    let (ack_tx, ack_rx) = bounded::<WakeAck>(1);
    let worker = spawn_scale(
        || "cq-wake-worker".to_owned(),
        STACK_BYTES,
        move || -> Result<(), &'static str> {
            #[cfg(all(feature = "mq-backend", target_os = "nuttx"))]
            let mut selection = {
                let mut selection = Select::new();
                selection.recv(&rx0);
                selection.recv(&rx1);
                selection
            };
            for _ in 0..WAKE_WARMUP + trials {
                #[cfg(all(feature = "mq-backend", target_os = "nuttx"))]
                let probe = {
                    let selected = selection.select();
                    selected
                        .recv(if selected.index() == 0 { &rx0 } else { &rx1 })
                        .map_err(|_| "wake input disconnected")?
                };
                #[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
                let probe = crossbeam_channel::select! {
                    recv(rx0) -> result => result,
                    recv(rx1) -> result => result,
                }
                .map_err(|_| "wake input disconnected")?;
                ack_tx
                    .send(WakeAck {
                        sent_cycles: probe.sent_cycles,
                        received_cycles: cycles(),
                    })
                    .map_err(|_| "wake output disconnected")?;
            }
            Ok(())
        },
    )
    .map_err(|_| "wake worker spawn failed")?;
    let mut inbound = Vec::with_capacity(trials);
    let mut outbound = Vec::with_capacity(trials);
    let mut sleep_clock_us = 0;
    for index in 0..WAKE_WARMUP + trials {
        // The previous acknowledgement ensures both input queues are empty;
        // sleeping allows the worker to block in select before each send.
        let sleep_started = cycles();
        thread::sleep(Duration::from_millis(sleep_ms));
        let sleep_delta = micros(cycles().wrapping_sub(sleep_started));
        if index == 0 {
            sleep_clock_us = sleep_delta;
        }
        let probe = WakeProbe {
            sent_cycles: cycles(),
        };
        #[cfg(all(feature = "mq-backend", target_os = "nuttx"))]
        (if index % 2 == 0 { &tx0 } else { &tx1 })
            .send(probe)
            .map_err(|_| "wake input send failed")?;
        #[cfg(not(all(feature = "mq-backend", target_os = "nuttx")))]
        match (if index % 2 == 0 { &tx0 } else { &tx1 }).try_send(probe) {
            Ok(()) => {}
            Err(_) => return Err("wake input unexpectedly full or disconnected"),
        }
        let ack = ack_rx.recv().map_err(|_| "wake output disconnected")?;
        let completed_cycles = cycles();
        if ack.sent_cycles != probe.sent_cycles {
            return Err("wake acknowledgement mismatch");
        }
        if index >= WAKE_WARMUP {
            inbound.push(micros(ack.received_cycles.wrapping_sub(ack.sent_cycles)));
            outbound.push(micros(completed_cycles.wrapping_sub(ack.received_cycles)));
        }
    }
    join_scale(worker).map_err(|_| "wake worker panicked")??;
    let inbound_max_us = *inbound.iter().max().ok_or("no wake samples")?;
    let outbound_p99_us = percentile(&mut outbound, 99);
    let inbound_p50_us = percentile(&mut inbound, 50);
    let inbound_p99_us = percentile(&mut inbound, 99);
    Ok(WakeReport {
        sleep_clock_us,
        inbound_p50_us,
        inbound_p99_us,
        inbound_max_us,
        outbound_p99_us,
    })
}

#[cfg(all(feature = "flash-thread-only", feature = "flash-mq-probe"))]
compile_error!("flash-thread-only and flash-mq-probe are separate ladder rungs");

#[cfg(feature = "flash-mq-probe")]
fn run_flash_mq_probe() -> Result<u32, &'static str> {
    let (sender, receiver) = bounded::<Ack>(1);
    let mut selection = Select::new();
    selection.recv(&receiver);
    let sent = Ack {
        lane: 1,
        producer: 2,
        sequence: 3,
        checksum: 0xa5a5_a5a5,
        sent_cycles: 4,
        received_cycles: 5,
        #[cfg(feature = "packet-service")]
        filtered: [0; 3],
    };
    sender.send(sent).map_err(|_| "flash probe send failed")?;
    let selected = selection.select();
    let selected_index = selected.index();
    let received = selected
        .recv(&receiver)
        .map_err(|_| "flash probe receive failed")?;
    if selected_index != 0
        || received.lane != sent.lane
        || received.producer != sent.producer
        || received.sequence != sent.sequence
        || received.checksum != sent.checksum
        || received.sent_cycles != sent.sent_cycles
        || received.received_cycles != sent.received_cycles
    {
        return Err("flash probe message mismatch");
    }
    Ok(received.checksum)
}

#[cfg(feature = "flash-report-probe")]
fn print_flash_report(checksum: u32) {
    let mut samples = [0u32; 64];
    let seed = cycles();
    for (index, sample) in samples.iter_mut().enumerate() {
        *sample = seed ^ (63 - index) as u32;
    }
    let p99 = percentile(&mut samples, 99);
    println!("CQ_FLASH_REPORT_PASS checksum={checksum} p99={p99} samples=64");
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CommandMode {
    Default,
    Threads,
    Large,
    Unknown,
}

#[cfg(any(
    not(all(feature = "ffi-scale-entry", target_os = "nuttx", not(test))),
    test
))]
fn parse_mode(mode: Option<&str>) -> CommandMode {
    match mode {
        None => CommandMode::Default,
        Some("threads") => CommandMode::Threads,
        Some("large") => CommandMode::Large,
        Some(_) => CommandMode::Unknown,
    }
}

fn dispatch(mode: CommandMode) -> bool {
    #[cfg(feature = "flash-thread-only")]
    {
        let _ = mode;
        return match run_thread_baseline() {
            Ok((before, live, after)) => {
                println!(
                    "CQ_THREADS_PASS threads={} heap_before={before} heap_live={live} heap_after={after}",
                    LARGE_WORKERS
                        + PRODUCERS
                        + 1
                );
                true
            }
            Err(reason) => {
                let _ = writeln!(io::stderr(), "CQ_THREADS_FAIL reason={reason}");
                false
            }
        };
    }

    #[cfg(all(not(feature = "flash-thread-only"), feature = "flash-mq-probe"))]
    {
        let _ = mode;
        match run_thread_baseline() {
            Ok((before, live, after)) => println!(
                "CQ_THREADS_PASS threads={} heap_before={before} heap_live={live} heap_after={after}",
                LARGE_WORKERS + PRODUCERS + 1
            ),
            Err(reason) => {
                let _ = writeln!(io::stderr(), "CQ_THREADS_FAIL reason={reason}");
                return false;
            }
        }
        return match run_flash_mq_probe() {
            Ok(checksum) => {
                println!("CQ_FLASH_MQ_PASS");
                #[cfg(feature = "flash-report-probe")]
                print_flash_report(checksum);
                #[cfg(not(feature = "flash-report-probe"))]
                let _ = checksum;
                true
            }
            Err(reason) => {
                let _ = writeln!(io::stderr(), "CQ_FLASH_MQ_FAIL reason={reason}");
                false
            }
        };
    }

    #[cfg(not(any(feature = "flash-thread-only", feature = "flash-mq-probe")))]
    {
        if mode == CommandMode::Threads {
            return match run_thread_baseline() {
                Ok((before, live, after)) => {
                    println!(
                        "CQ_THREADS_PASS threads={} heap_before={before} heap_live={live} heap_after={after}",
                        LARGE_WORKERS
                            + PRODUCERS
                            + 1
                    );
                    true
                }
                Err(reason) => {
                    let _ = writeln!(io::stderr(), "CQ_THREADS_FAIL reason={reason}");
                    false
                }
            };
        }
        if mode == CommandMode::Unknown {
            let _ = writeln!(io::stderr(), "CQ_SCALE_FAIL unknown mode");
            return false;
        }
        for config in [
            Config {
                label: "small",
                workers: 1,
                lanes_per_worker: 2,
                sequences: SMALL_SEQUENCES,
            },
            Config {
                label: "medium",
                workers: MEDIUM_WORKERS,
                lanes_per_worker: 2,
                sequences: MEDIUM_SEQUENCES,
            },
            Config {
                label: "large",
                workers: LARGE_WORKERS,
                lanes_per_worker: LARGE_LANES_PER_WORKER,
                sequences: LARGE_SEQUENCES,
            },
        ] {
            if mode == CommandMode::Large && config.name() != "large" {
                continue;
            }
            match run(config) {
                Ok(report) => print_report(&report),
                Err(reason) => {
                    let _ = writeln!(
                        io::stderr(),
                        "CQ_SCALE_FAIL mode={} reason={reason}",
                        config.name()
                    );
                    return false;
                }
            }
        }
        match run_wake(WAKE_TRIALS, 20) {
            Ok(report) => println!(
                "CQ_WAKE_PASS trials={WAKE_TRIALS} queues=3 threads=2 sleep_clock_us={} rx_p50_us={} rx_p99_us={} rx_max_us={} ack_p99_us={}",
                report.sleep_clock_us,
                report.inbound_p50_us,
                report.inbound_p99_us,
                report.inbound_max_us,
                report.outbound_p99_us
            ),
            Err(reason) => {
                let _ = writeln!(io::stderr(), "CQ_WAKE_FAIL reason={reason}");
                return false;
            }
        }
        true
    }
}

#[cfg(not(all(feature = "ffi-scale-entry", target_os = "nuttx", not(test))))]
fn main() -> ExitCode {
    let mode = parse_mode(std::env::args().nth(1).as_deref());
    if dispatch(mode) {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

#[cfg(all(feature = "ffi-scale-entry", any(target_os = "nuttx", test)))]
unsafe fn command_mode_from_c(argc: c_int, argv: *const *const c_char) -> CommandMode {
    if argc < 2 || argv.is_null() {
        return CommandMode::Default;
    }
    // SAFETY: the C runtime's argc/argv contract provides argv[1] when argc is
    // at least two; the pointer is read only for this synchronous dispatch.
    let argument = unsafe { *argv.add(1) };
    if argument.is_null() {
        return CommandMode::Default;
    }
    // SAFETY: argv entries are NUL-terminated strings for the duration of main.
    match unsafe { CStr::from_ptr(argument) }.to_bytes() {
        b"threads" => CommandMode::Threads,
        b"large" => CommandMode::Large,
        _ => CommandMode::Unknown,
    }
}

#[cfg(all(feature = "ffi-scale-entry", target_os = "nuttx", not(test)))]
#[unsafe(no_mangle)]
/// # Safety
/// Non-null argv must contain argc live C string pointers supplied by NSH.
pub unsafe extern "C" fn main(argc: c_int, argv: *const *const c_char) -> c_int {
    #[cfg(feature = "paired-scale-control")]
    {
        let _ = (argc, argv);
        return paired_control();
    }
    #[cfg(not(feature = "paired-scale-control"))]
    {
        // SAFETY: the caller supplies the C argc/argv contract described above.
        if dispatch(unsafe { command_mode_from_c(argc, argv) }) {
            0
        } else {
            1
        }
    }
}

#[cfg(all(feature = "paired-scale-control", target_os = "nuttx", not(test)))]
fn paired_control() -> c_int {
    unsafe extern "C" {
        fn nxrs_c_scale_control_main(argc: c_int, argv: *const *const c_char) -> c_int;
    }
    let argv = [c"control".as_ptr(), c"large".as_ptr(), std::ptr::null()];
    for batch in 0..4 {
        println!("CQ_PAIR_BEGIN batch={batch} first={}", batch % 2);
        for offset in 0..2 {
            let language = (batch + offset) % 2;
            let passed = if language == 0 {
                // SAFETY: live NUL-terminated immutable arguments; C only reads
                // them synchronously and joins all its workers before return.
                unsafe { nxrs_c_scale_control_main(2, argv.as_ptr()) == 0 }
            } else {
                match run(Config {
                    label: "large",
                    workers: LARGE_WORKERS,
                    lanes_per_worker: LARGE_LANES_PER_WORKER,
                    sequences: LARGE_SEQUENCES,
                }) {
                    Ok(report) => {
                        print_report(&report);
                        true
                    }
                    Err(_) => false,
                }
            };
            if !passed {
                println!("CQ_PAIR_FAIL batch={batch}");
                return 1;
            }
        }
    }
    println!("CQ_PAIR_PASS batches=4 messages=5760 threads=20 queues=60");
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_modes_preserve_default_and_named_dispatch() {
        assert_eq!(parse_mode(None), CommandMode::Default);
        assert_eq!(parse_mode(Some("threads")), CommandMode::Threads);
        assert_eq!(parse_mode(Some("large")), CommandMode::Large);
        assert_eq!(parse_mode(Some("small")), CommandMode::Unknown);
    }

    #[cfg(feature = "ffi-scale-entry")]
    #[test]
    fn c_argument_parser_borrows_valid_strings_and_handles_empty_arguments() {
        use std::ffi::CString;
        let program = CString::new("cq_scale").unwrap();
        for (argument, expected) in [
            ("threads", CommandMode::Threads),
            ("large", CommandMode::Large),
            ("unknown", CommandMode::Unknown),
        ] {
            let argument = CString::new(argument).unwrap();
            let argv = [program.as_ptr(), argument.as_ptr(), std::ptr::null()];
            // SAFETY: both CStrings and the argv array stay alive for the call.
            assert_eq!(unsafe { command_mode_from_c(2, argv.as_ptr()) }, expected);
        }
        // SAFETY: the parser does not dereference absent argument pointers.
        assert_eq!(
            unsafe { command_mode_from_c(0, std::ptr::null()) },
            CommandMode::Default
        );
        let argv = [program.as_ptr(), std::ptr::null()];
        // SAFETY: argv[1] exists and is explicitly null.
        assert_eq!(
            unsafe { command_mode_from_c(2, argv.as_ptr()) },
            CommandMode::Default
        );
    }

    #[cfg(feature = "flash-mq-probe")]
    #[test]
    fn flash_mq_probe_round_trips_checked_wire_message() {
        assert_eq!(run_flash_mq_probe().unwrap(), 0xa5a5_a5a5);
    }

    #[cfg(all(not(feature = "wire-only"), not(feature = "packet-service")))]
    #[test]
    fn event_payload_matches_c_wire_formula() {
        let event = Event::new(7, 2, 5);
        for (index, byte) in event.payload.iter().enumerate() {
            let expected = 7u8.wrapping_mul(17).wrapping_add(2).wrapping_add(5) ^ index as u8;
            assert_eq!(*byte, expected);
        }
        assert!(event.valid());
    }

    #[cfg(all(
        not(feature = "wire-only"),
        not(feature = "legacy-payload-copy"),
        not(feature = "packet-service")
    ))]
    #[test]
    fn reused_event_overwrites_every_payload_byte_and_header() {
        let mut event = Event::empty();
        for (lane, producer, sequence) in [(255, 3, 65535), (0, 0, 1), (7, 2, 261)] {
            event.payload.fill(0x5a);
            event.sent_cycles = u32::MAX;
            event.reset(lane, producer, sequence);
            assert_eq!(
                (event.lane, event.producer, event.sequence),
                (lane as u8, producer as u8, sequence)
            );
            assert_eq!(event.sent_cycles, 0);
            let seed = (lane as u8)
                .wrapping_mul(17)
                .wrapping_add(producer as u8)
                .wrapping_add(sequence as u8);
            let mut expected_sum =
                (lane as u32) << 24 | (producer as u32) << 16 | u32::from(sequence);
            for (index, byte) in event.payload.iter().enumerate() {
                assert_eq!(*byte, seed ^ index as u8);
                expected_sum = expected_sum.rotate_left(3).wrapping_add(u32::from(*byte));
            }
            assert_eq!(event.checksum, expected_sum);
            assert!(event.valid());
            event.payload[235] ^= 1;
            assert!(!event.valid());
        }
    }

    #[test]
    fn payload_and_small_topology() {
        assert_eq!(std::mem::size_of::<Event>(), 248);
        let report = run(Config {
            label: "small",
            workers: 1,
            lanes_per_worker: 2,
            sequences: 8,
        })
        .unwrap();
        assert_eq!(report.config.events(), 64);
        assert!(report.inbound_p99_us >= report.inbound_p50_us);
    }

    #[test]
    fn large_topology() {
        let report = run(Config {
            label: "large",
            workers: LARGE_WORKERS,
            lanes_per_worker: LARGE_LANES_PER_WORKER,
            sequences: 8,
        })
        .unwrap();
        assert_eq!(
            report.config.events(),
            report.config.lanes() * PRODUCERS * 8
        );
        assert_eq!(report.config.logical_streams(), 60);
        assert_eq!(report.config.samples(), report.config.lanes() * PRODUCERS);
        assert_eq!(
            report.config.queues(),
            if cfg!(feature = "compact-topology") {
                10
            } else if cfg!(feature = "multiplexed") {
                30
            } else {
                60
            }
        );
        assert!(report.full > 0);
    }

    #[test]
    fn partial_sample_interval_counts_each_lane() {
        let config = Config {
            label: "partial",
            workers: 5,
            lanes_per_worker: 11,
            sequences: 26,
        };
        assert_eq!(config.events(), 5_720);
        assert_eq!(config.samples(), 880);
    }

    #[test]
    fn medium_topology() {
        let report = run(Config {
            label: "medium",
            workers: MEDIUM_WORKERS,
            lanes_per_worker: 2,
            sequences: 8,
        })
        .unwrap();
        assert_eq!(report.config.events(), 768);
        assert_eq!(
            report.config.queues(),
            if cfg!(feature = "multiplexed") {
                24
            } else {
                36
            }
        );
    }

    #[test]
    fn idle_to_wakeup_probe() {
        let report = run_wake(8, 1).unwrap();
        assert!(report.inbound_p99_us >= report.inbound_p50_us);
    }

    #[test]
    fn thread_baseline_releases_all_threads() {
        let (before, live, after) = run_thread_baseline().unwrap();
        assert!(live >= before);
        assert!(live >= after);
    }

    #[cfg(feature = "wire-only")]
    #[test]
    fn wire_mode_checks_message_sentinels() {
        let mut event = Event::new(2, 1, 3);
        assert!(event.valid());
        event.payload[235] = 0;
        assert!(!event.valid());
    }
}

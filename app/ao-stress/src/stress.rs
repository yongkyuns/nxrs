//! App-owned active objects over the existing event inbox, not a new runtime.
use event::{bounded, EventInbox, EventSender};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, SyncSender, TrySendError};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const MAX: usize = 8;
const STACK_BYTES: usize = 32768;
const TICK: Duration = Duration::from_millis(5);

#[derive(Clone, Debug)]
pub(super) struct Config {
    pub duration: Duration,
    pub shutdown: Duration,
    pub deadline: Duration,
    pub producers: usize,
    pub workers: usize,
    pub capacity: usize,
    pub work: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            duration: Duration::from_millis(300),
            shutdown: Duration::from_secs(2),
            deadline: Duration::from_millis(20),
            producers: 2,
            workers: 2,
            capacity: 16,
            work: 32,
        }
    }
}

impl Config {
    pub fn validate(&self) -> Result<(), String> {
        if !(1..=MAX).contains(&self.producers)
            || !(1..=MAX).contains(&self.workers)
            || !(1..=256).contains(&self.capacity)
            || self.work > 100_000
            || self.duration < Duration::from_millis(10)
            || self.duration > Duration::from_secs(60)
            || self.shutdown < Duration::from_millis(10)
            || self.shutdown > Duration::from_secs(10)
            || self.deadline < Duration::from_micros(1)
            || self.deadline > Duration::from_secs(10)
        {
            return Err(
                "configuration out of range; use --help (large topologies may exceed MCU RAM)"
                    .into(),
            );
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug)]
pub(super) enum Scenario {
    Steady,
    Burst,
    SlowConsumer,
    CpuLoad,
}
impl Scenario {
    pub const ALL: [Self; 4] = [Self::Steady, Self::Burst, Self::SlowConsumer, Self::CpuLoad];
    pub(super) fn name(self) -> &'static str {
        match self {
            Self::Steady => "steady",
            Self::Burst => "burst",
            Self::SlowConsumer => "slow-consumer",
            Self::CpuLoad => "cpu-load",
        }
    }
    fn pacing(self) -> Duration {
        match self {
            Self::Steady => Duration::from_millis(1),
            Self::SlowConsumer => Duration::from_micros(100),
            _ => Duration::ZERO,
        }
    }
}

#[derive(Clone, Copy)]
struct Message {
    producer: usize,
    worker: usize,
    sequence: u64,
    created: Instant,
    checksum: u64,
}
fn checksum(producer: usize, worker: usize, sequence: u64) -> u64 {
    sequence.wrapping_mul(0x9e3779b97f4a7c15)
        ^ (producer as u64).rotate_left(17)
        ^ (worker as u64).rotate_left(41)
}

#[derive(Default, Clone, Debug)]
struct Histogram {
    bins: [u64; 32],
    samples: u64,
    maximum: u64,
    misses: u64,
}
impl Histogram {
    fn record(&mut self, duration: Duration, deadline: Duration) {
        // Round UP before bucketing so sub-microsecond fractions cannot make
        // a reported percentile upper bound smaller than the measured latency.
        let us = u64::try_from(duration.as_nanos().div_ceil(1000)).unwrap_or(u64::MAX);
        let bin = if us <= 1 {
            0
        } else {
            (64 - (us - 1).leading_zeros()) as usize
        }
        .min(31);
        self.bins[bin] += 1;
        self.samples += 1;
        self.maximum = self.maximum.max(us);
        self.misses += u64::from(duration > deadline);
    }
    // Upper bounds, not exact sample percentiles; zero samples are not latency=0.
    fn percentile(&self, percentage: u64) -> Option<u64> {
        if self.samples == 0 {
            return None;
        }
        let rank = (self.samples * percentage).div_ceil(100);
        let mut count = 0;
        for (index, &samples) in self.bins.iter().enumerate() {
            count += samples;
            if count >= rank {
                return Some(if index == 31 {
                    self.maximum.max(1u64 << 31)
                } else {
                    1u64 << index
                });
            }
        }
        None
    }
}
fn micros(duration: Duration) -> u64 {
    u64::try_from(duration.as_micros()).unwrap_or(u64::MAX)
}

#[derive(Default, Debug)]
struct Producer {
    id: usize,
    attempts: u64,
    accepted: u64,
    full: u64,
    disconnected: u64,
    digest: u64,
}
#[derive(Default, Debug)]
struct Worker {
    id: usize,
    handled: u64,
    forwarded: u64,
    full: u64,
    disconnected: u64,
    errors: u64,
    input_digest: u64,
    output_digest: u64,
    ticks: u64,
    max_late_us: u64,
}
#[derive(Default, Debug)]
struct Collector {
    received: u64,
    errors: u64,
    digest: u64,
    latency: Histogram,
}
// Keep the fixed histogram inline in a setup-allocated, owner-count-bounded
// completion queue rather than adding a heap allocation when an owner exits.
#[allow(clippy::large_enum_variant)]
#[derive(Debug)]
enum Outcome {
    Producer(Producer),
    Worker(Worker),
    Collector(Collector),
}

// Only startup/cancellation are shared. Work state and statistics stay private
// to each owner, and are moved to the app after termination.
type Gate = Arc<(Mutex<Option<Instant>>, Condvar)>;
type CompletionGate = Arc<(Mutex<bool>, Condvar)>;
struct Threads {
    cancel: Arc<AtomicBool>,
    gate: Gate,
    reports: Option<SyncSender<Outcome>>,
    completion_ready: Option<SyncSender<()>>,
    completion_gate: Option<CompletionGate>,
    handles: Vec<JoinHandle<()>>,
}
impl Threads {
    fn new(count: usize) -> (Self, Receiver<Outcome>) {
        let (threads, reports, ready) = Self::new_inner(count, false);
        debug_assert!(ready.is_none());
        (threads, reports)
    }

    fn observed(count: usize) -> (Self, Receiver<Outcome>, Receiver<()>) {
        let (threads, reports, ready) = Self::new_inner(count, true);
        (
            threads,
            reports,
            ready.expect("observed threads have completion receiver"),
        )
    }

    fn new_inner(count: usize, observed: bool) -> (Self, Receiver<Outcome>, Option<Receiver<()>>) {
        let (reports, receiver) = sync_channel(count);
        let (completion_ready, ready_receiver, completion_gate) = if observed {
            let (sender, receiver) = sync_channel(count);
            (
                Some(sender),
                Some(receiver),
                Some(Arc::new((Mutex::new(false), Condvar::new()))),
            )
        } else {
            (None, None, None)
        };
        (
            Self {
                cancel: Arc::new(AtomicBool::new(false)),
                gate: Arc::new((Mutex::new(None), Condvar::new())),
                reports: Some(reports),
                completion_ready,
                completion_gate,
                handles: Vec::with_capacity(count),
            },
            receiver,
            ready_receiver,
        )
    }
    fn spawn<F>(&mut self, name: String, body: F) -> Result<(), String>
    where
        F: FnOnce(Instant, &AtomicBool) -> Outcome + Send + 'static,
    {
        let cancel = self.cancel.clone();
        let gate = self.gate.clone();
        let reports = self.reports.as_ref().unwrap().clone();
        let completion_ready = self.completion_ready.clone();
        let completion_gate = self.completion_gate.clone();
        let handle = thread::Builder::new()
            .name(name)
            .stack_size(STACK_BYTES)
            .spawn(move || {
                let (lock, changed) = &*gate;
                let mut ready = lock.lock().unwrap();
                while ready.is_none() && !cancel.load(Ordering::Acquire) {
                    ready = changed.wait(ready).unwrap();
                }
                if cancel.load(Ordering::Acquire) {
                    return;
                }
                let start = ready.unwrap();
                drop(ready);

                let outcome = body(start, &cancel);
                if let (Some(ready), Some(gate)) = (completion_ready, completion_gate) {
                    if ready.try_send(()).is_err() {
                        return;
                    }
                    let (lock, changed) = &*gate;
                    let mut released = lock.lock().unwrap();
                    while !*released && !cancel.load(Ordering::Acquire) {
                        released = changed.wait(released).unwrap();
                    }
                    if cancel.load(Ordering::Acquire) {
                        return;
                    }
                }

                // One result per thread; this preallocated channel has count slots.
                let _ = reports.try_send(outcome);
            })
            .map_err(|error| format!("thread creation failed: {error}"))?;
        self.handles.push(handle);
        Ok(())
    }
    fn release(&mut self) -> Instant {
        let start = Instant::now();
        *self.gate.0.lock().unwrap() = Some(start);
        self.gate.1.notify_all();
        self.reports.take();
        start
    }
    fn release_completion(&self) {
        if let Some(gate) = &self.completion_gate {
            let (lock, changed) = &**gate;
            *lock.lock().unwrap_or_else(|error| error.into_inner()) = true;
            changed.notify_all();
        }
    }

    fn join_all(&mut self) -> Result<(), String> {
        let mut panicked = false;
        for handle in self.handles.drain(..) {
            panicked |= handle.join().is_err();
        }
        if panicked {
            Err("an active-object thread panicked".into())
        } else {
            Ok(())
        }
    }
}
impl Drop for Threads {
    fn drop(&mut self) {
        // Includes partial startup failure and timeout: unblock startup waiters
        // and cooperatively stop owners. No detached survivors between rounds.
        // This is not a hard-real-time watchdog: OS scheduling can delay join.
        self.cancel.store(true, Ordering::Release);
        // Synchronize with the predicate check so cancellation cannot lose a
        // notification between checking the flag and entering Condvar::wait.
        let guard = self.gate.0.lock().unwrap_or_else(|e| e.into_inner());
        self.gate.1.notify_all();
        drop(guard);
        self.release_completion();
        let _ = self.join_all();
    }
}

fn produce(
    id: usize,
    inputs: Vec<EventSender<Message>>,
    config: &Config,
    scenario: Scenario,
    start: Instant,
    cancel: &AtomicBool,
) -> Producer {
    let mut stats = Producer {
        id,
        ..Producer::default()
    };
    let end = start + config.duration;
    while !cancel.load(Ordering::Acquire) && Instant::now() < end {
        let sequence = stats.attempts;
        let worker = (sequence % inputs.len() as u64) as usize;
        let message = Message {
            producer: id,
            worker,
            sequence,
            created: Instant::now(),
            checksum: checksum(id, worker, sequence),
        };
        stats.attempts += 1;
        match inputs[worker].try_send(message) {
            Ok(()) => {
                stats.accepted += 1;
                stats.digest = stats.digest.wrapping_add(message.checksum);
            }
            Err(TrySendError::Full(_)) => stats.full += 1,
            Err(TrySendError::Disconnected(_)) => {
                stats.disconnected += 1;
                break;
            }
        }
        if !scenario.pacing().is_zero() {
            thread::sleep(
                scenario
                    .pacing()
                    .min(end.saturating_duration_since(Instant::now())),
            );
        } else if stats.attempts.is_multiple_of(64) {
            thread::yield_now();
        }
    }
    stats // Dropping every producer sender permits a natural drain/disconnect.
}

fn work(
    id: usize,
    inbox: EventInbox<Message>,
    output: EventSender<Message>,
    config: &Config,
    scenario: Scenario,
    start: Instant,
    cancel: &AtomicBool,
) -> Worker {
    let mut stats = Worker {
        id,
        ..Worker::default()
    };
    let mut previous = [None; MAX];
    let mut next_tick = start + TICK;
    let iterations = if matches!(scenario, Scenario::CpuLoad) {
        config.work.max(4096)
    } else {
        config.work
    };
    while !cancel.load(Ordering::Acquire) {
        let now = Instant::now();
        if now >= next_tick {
            stats.ticks += 1;
            stats.max_late_us = stats.max_late_us.max(micros(now - next_tick));
            next_tick = now + TICK; // Skip missed periods; no unbounded catch-up loop.
        }
        // One wait point, even under load. Timers are checked before every
        // receive, not only on timeout (continuous traffic must not starve them).
        let message = match inbox.wait(Some(next_tick.saturating_duration_since(Instant::now()))) {
            Ok(message) => message,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => break,
        };
        stats.handled += 1;
        stats.input_digest = stats.input_digest.wrapping_add(message.checksum);
        if message.producer >= config.producers
            || message.worker != id
            || message.checksum != checksum(message.producer, id, message.sequence)
        {
            stats.errors += 1;
        } else {
            if previous[message.producer].is_some_and(|last| message.sequence <= last) {
                stats.errors += 1;
            }
            previous[message.producer] = Some(message.sequence);
        }
        let mut value = message.checksum;
        for iteration in 0..iterations {
            value = std::hint::black_box(value.wrapping_mul(1664525).wrapping_add(1013904223));
            if iteration.is_multiple_of(256) && cancel.load(Ordering::Acquire) {
                break;
            }
        }
        std::hint::black_box(value);
        // Handlers never block waiting for another AO's queue capacity.
        match output.try_send(message) {
            Ok(()) => {
                stats.forwarded += 1;
                stats.output_digest = stats.output_digest.wrapping_add(message.checksum);
            }
            Err(TrySendError::Full(_)) => stats.full += 1,
            Err(TrySendError::Disconnected(_)) => {
                stats.disconnected += 1;
                break;
            }
        }
    }
    stats
}

fn collect(
    inbox: EventInbox<Message>,
    config: &Config,
    scenario: Scenario,
    cancel: &AtomicBool,
) -> Collector {
    let mut stats = Collector::default();
    let mut previous = [None; MAX * MAX];
    while !cancel.load(Ordering::Acquire) {
        let message = match inbox.wait(Some(TICK)) {
            Ok(message) => message,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => break,
        };
        stats.received += 1;
        stats.digest = stats.digest.wrapping_add(message.checksum);
        if message.producer >= config.producers
            || message.worker >= config.workers
            || message.checksum != checksum(message.producer, message.worker, message.sequence)
        {
            stats.errors += 1;
        } else {
            let lane = message.worker * MAX + message.producer;
            if previous[lane].is_some_and(|last| message.sequence <= last) {
                stats.errors += 1;
            }
            previous[lane] = Some(message.sequence);
        }
        stats
            .latency
            .record(message.created.elapsed(), config.deadline);
        if matches!(scenario, Scenario::SlowConsumer) {
            // Deliberately blocking I/O surrogate, isolated to this owner.
            thread::sleep(Duration::from_millis(1));
        }
    }
    stats
}

#[derive(Default, Debug)]
pub(super) struct Report {
    producers: Vec<Producer>,
    workers: Vec<Worker>,
    collector: Collector,
    elapsed: Duration,
    shutdown: Duration,
}
impl Report {
    fn validate(&self, config: &Config) -> Result<(), String> {
        let accepted: u64 = self.producers.iter().map(|p| p.accepted).sum();
        let handled: u64 = self.workers.iter().map(|w| w.handled).sum();
        let forwarded: u64 = self.workers.iter().map(|w| w.forwarded).sum();
        let ingress_digest = self
            .producers
            .iter()
            .fold(0u64, |s, p| s.wrapping_add(p.digest));
        let handled_digest = self
            .workers
            .iter()
            .fold(0u64, |s, w| s.wrapping_add(w.input_digest));
        let output_digest = self
            .workers
            .iter()
            .fold(0u64, |s, w| s.wrapping_add(w.output_digest));
        let mut pids = [false; MAX];
        let mut wids = [false; MAX];
        let unique_producers = self.producers.iter().all(|p| {
            if p.id >= config.producers || pids[p.id] {
                return false;
            }
            pids[p.id] = true;
            true
        });
        let unique_workers = self.workers.iter().all(|w| {
            if w.id >= config.workers || wids[w.id] {
                return false;
            }
            wids[w.id] = true;
            true
        });
        if !unique_producers
            || !unique_workers
            || self.producers.len() != config.producers
            || self.workers.len() != config.workers
            || self
                .producers
                .iter()
                .any(|p| p.attempts != p.accepted + p.full || p.disconnected != 0)
            || self
                .workers
                .iter()
                .any(|w| w.handled != w.forwarded + w.full || w.errors != 0 || w.disconnected != 0)
            || accepted != handled
            || forwarded != self.collector.received
            || ingress_digest != handled_digest
            || output_digest != self.collector.digest
            || self.collector.errors != 0
            || self.collector.received == 0
        {
            return Err(format!(
                "accounting/order/integrity/progress invariant failed: {self:?}"
            ));
        }
        Ok(())
    }
    pub fn print(&self, config: &Config, scenario: Scenario, round: usize) {
        let attempted: u64 = self.producers.iter().map(|p| p.attempts).sum();
        let accepted: u64 = self.producers.iter().map(|p| p.accepted).sum();
        let handled: u64 = self.workers.iter().map(|w| w.handled).sum();
        let forwarded: u64 = self.workers.iter().map(|w| w.forwarded).sum();
        println!("AO_CONFIG scenario={} producers={} workers={} capacity={} duration_ms={} deadline_us={} shutdown_budget_ms={} configured_work={} effective_work={} os={} arch={}",
            scenario.name(), config.producers, config.workers, config.capacity, config.duration.as_millis(),
            config.deadline.as_micros(), config.shutdown.as_millis(), config.work,
            if matches!(scenario, Scenario::CpuLoad) { config.work.max(4096) } else { config.work },
            std::env::consts::OS, std::env::consts::ARCH);
        let latency = &self.collector.latency;
        let throughput =
            self.collector.received.saturating_mul(1_000_000) / micros(self.elapsed).max(1);
        let slots = config.capacity * (config.workers + 1);
        // Static payload envelope bound; excludes thread stacks, allocator and
        // std channel bookkeeping. Not a measured queue high-water or total RAM.
        let payload_bound =
            (slots + config.producers + config.workers + 1) * std::mem::size_of::<Message>();
        println!("AO_RESULT {{\"scenario\":\"{}\",\"round\":{},\"attempted\":{},\"accepted\":{},\"ingress_full\":{},\"handled\":{},\"forwarded\":{},\"egress_full\":{},\"received\":{},\"integrity_errors\":0,\"elapsed_us\":{},\"shutdown_us\":{},\"throughput_per_s\":{},\"p50_upper_us\":{},\"p95_upper_us\":{},\"p99_upper_us\":{},\"max_latency_us\":{},\"deadline_misses\":{},\"timer_ticks\":{},\"max_timer_late_us\":{},\"queue_slots\":{},\"payload_bound_bytes\":{},\"owner_stack_request_bytes\":{},\"joined\":true}}",
            scenario.name(), round, attempted, accepted, attempted - accepted, handled,
            forwarded, handled - forwarded, self.collector.received, micros(self.elapsed),
            micros(self.shutdown), throughput, latency.percentile(50).unwrap(),
            latency.percentile(95).unwrap(), latency.percentile(99).unwrap(), latency.maximum,
            latency.misses, self.workers.iter().map(|w| w.ticks).sum::<u64>(),
            self.workers.iter().map(|w| w.max_late_us).max().unwrap_or(0), slots,
            payload_bound, (config.producers + config.workers + 1) * STACK_BYTES);
        for producer in &self.producers {
            println!(
                "AO_PRODUCER id={} attempted={} accepted={} full={}",
                producer.id, producer.attempts, producer.accepted, producer.full
            );
        }
        for worker in &self.workers {
            println!(
                "AO_WORKER id={} handled={} forwarded={} full={} timer_ticks={}",
                worker.id, worker.handled, worker.forwarded, worker.full, worker.ticks
            );
        }
    }
}

pub(super) fn run(config: &Config, scenario: Scenario) -> Result<Report, String> {
    run_inner(config, scenario, None)
}

#[cfg(any(test, target_os = "nuttx"))]
pub(super) fn run_observed<F>(
    config: &Config,
    scenario: Scenario,
    observer: &mut F,
) -> Result<Report, String>
where
    F: FnMut() -> Result<(), String>,
{
    run_inner(config, scenario, Some(observer))
}

fn run_inner(
    config: &Config,
    scenario: Scenario,
    observer: Option<&mut dyn FnMut() -> Result<(), String>>,
) -> Result<Report, String> {
    config.validate()?;
    let count = config.producers + config.workers + 1;
    let observed = observer.is_some();
    let (mut threads, reports, completion_ready) = if observed {
        let (threads, reports, ready) = Threads::observed(count);
        (threads, reports, Some(ready))
    } else {
        let (threads, reports) = Threads::new(count);
        (threads, reports, None)
    };
    let (output, collector_inbox) = bounded(config.capacity);
    let cfg = config.clone();
    threads.spawn("ao-collector".into(), move |_, cancel| {
        Outcome::Collector(collect(collector_inbox, &cfg, scenario, cancel))
    })?;
    let mut inputs = Vec::with_capacity(config.workers);
    for id in 0..config.workers {
        let (sender, inbox) = bounded(config.capacity);
        inputs.push(sender);
        let output = output.clone();
        let cfg = config.clone();
        threads.spawn(format!("ao-worker-{id}"), move |start, cancel| {
            Outcome::Worker(work(id, inbox, output, &cfg, scenario, start, cancel))
        })?;
    }
    drop(output);
    for id in 0..config.producers {
        let inputs = inputs.clone();
        let cfg = config.clone();
        threads.spawn(format!("ao-source-{id}"), move |start, cancel| {
            Outcome::Producer(produce(id, inputs, &cfg, scenario, start, cancel))
        })?;
    }
    drop(inputs);
    // All queues, owner state, thread handles and sender sets are established
    // before starting the timed workload. There is no logging in handlers.
    let mut report = Report {
        producers: Vec::with_capacity(config.producers),
        workers: Vec::with_capacity(config.workers),
        ..Report::default()
    };
    let start = threads.release();
    let deadline = start + config.duration + config.shutdown;

    if let Some(ready) = completion_ready {
        for _ in 0..count {
            ready
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .map_err(|error| {
                    format!(
                        "completion-observer deadline/disconnect: {error}; cancelling and joining owners"
                    )
                })?;
        }
        if let Some(observe) = observer {
            observe()?;
        }
        threads.release_completion();
    }

    let mut collectors = 0;
    for _ in 0..count {
        match reports
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(|error| {
                format!("completion deadline/disconnect: {error}; cancelling and joining owners")
            })? {
            Outcome::Producer(stats) => report.producers.push(stats),
            Outcome::Worker(stats) => report.workers.push(stats),
            Outcome::Collector(stats) => {
                report.collector = stats;
                collectors += 1;
            }
        }
    }
    threads.join_all()?;
    report.elapsed = start.elapsed();
    report.shutdown = Instant::now().saturating_duration_since(start + config.duration);
    if collectors != 1 {
        return Err("missing or duplicate collector report".into());
    }
    report.validate(config)?;
    Ok(report)
}

pub(super) fn transport_edges() -> Result<(), String> {
    // Deterministically prove overload/disconnect handling, even on hosts where
    // a scheduler happens to drain every offered message in a timed scenario.
    let (sender, inbox) = bounded(2);
    sender.try_send(10).map_err(|_| "prefill failed")?;
    sender.try_send(20).map_err(|_| "prefill failed")?;
    if !matches!(sender.try_send(30), Err(TrySendError::Full(30))) {
        return Err("missing overload".into());
    }
    drop(sender);
    if inbox.wait(None) != Ok(10)
        || inbox.wait(None) != Ok(20)
        || inbox.wait(None) != Err(RecvTimeoutError::Disconnected)
    {
        return Err("drain ordering failed".into());
    }
    let (sender, inbox) = bounded(1);
    drop(inbox);
    if !matches!(sender.try_send(40), Err(TrySendError::Disconnected(40))) {
        return Err("missing disconnect".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_overload_and_disconnect() {
        transport_edges().unwrap();
    }
    #[test]
    fn histogram_upper_bounds_and_empty_samples() {
        let mut h = Histogram::default();
        assert_eq!(h.percentile(99), None);
        for us in [1, 2, 3, 8, 9] {
            h.record(Duration::from_micros(us), Duration::from_micros(4));
        }
        assert_eq!(h.percentile(50), Some(4));
        assert_eq!(h.percentile(99), Some(16));
        assert_eq!((h.maximum, h.misses), (9, 2));
    }
    #[test]
    fn histogram_rounds_fractional_microseconds_up() {
        let mut h = Histogram::default();
        h.record(Duration::from_nanos(1001), Duration::from_micros(10));
        assert_eq!(h.percentile(99), Some(2));
        assert_eq!(h.maximum, 2);
    }
    #[test]
    fn all_scenarios_conserve_messages() {
        let cfg = Config {
            duration: Duration::from_millis(40),
            ..Config::default()
        };
        for scenario in Scenario::ALL {
            run(&cfg, scenario).unwrap();
        }
    }
    #[test]
    fn tiny_inboxes_and_restarts() {
        let cfg = Config {
            capacity: 1,
            duration: Duration::from_millis(30),
            ..Config::default()
        };
        for _ in 0..3 {
            run(&cfg, Scenario::SlowConsumer).unwrap();
        }
    }
    #[test]
    fn completion_observer_runs_once_before_join() {
        let cfg = Config {
            duration: Duration::from_millis(30),
            ..Config::default()
        };
        let mut calls = 0;
        run_observed(&cfg, Scenario::Steady, &mut || {
            calls += 1;
            Ok(())
        })
        .unwrap();
        assert_eq!(calls, 1);
    }
    #[test]
    fn partial_startup_is_cancelled_and_joined() {
        let (mut threads, _reports) = Threads::new(1);
        threads
            .spawn("never-started".into(), |_, _| panic!("must not run"))
            .unwrap();
        drop(threads);
    }
    #[test]
    fn reject_invalid_budgets_before_spawning() {
        assert!(Config {
            capacity: 0,
            ..Config::default()
        }
        .validate()
        .is_err());
        assert!(Config {
            workers: MAX + 1,
            ..Config::default()
        }
        .validate()
        .is_err());
    }
    #[test]
    fn lost_and_reordered_messages_cannot_pass_validation() {
        let cfg = Config {
            duration: Duration::from_millis(30),
            ..Config::default()
        };
        let mut report = run(&cfg, Scenario::Steady).unwrap();
        report.collector.received += 1;
        assert!(report.validate(&cfg).is_err());
        report.collector.received -= 1;
        report.workers[0].errors += 1;
        assert!(report.validate(&cfg).is_err());
    }
}

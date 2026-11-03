//! Isolated std-channel qualification; never a production transport backend.
mod latency;
mod native;
mod witness;

use native::{Gate, Result, Schedule};
use std::{
    sync::{
        atomic::{AtomicI32, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const CAPACITY: usize = 8;
const WORKER_STACK: usize = 16 * 1024;
const SENDER_PRIORITY: i32 = 120;
const RECEIVER_PRIORITY: i32 = 180;
const OBSERVER_PRIORITY: i32 = 220;
const WATCHDOG_PRIORITY: i32 = 240;
const WAIT_MS: u32 = 1000;
const RUN_BUDGET_MS: u32 = 15000;

#[derive(Clone, Copy)]
struct Message([u32; 4]);
impl Message {
    fn new(sequence: u32) -> Self {
        Self([sequence, !sequence, sequence ^ 0x9173_ac5e, 0x1357_2468])
    }
    fn valid(self, sequence: u32) -> bool {
        self.0 == Self::new(sequence).0
    }
}

#[derive(Clone, Copy)]
enum Mode {
    Latency { samples: u32, warmup: u32 },
    Reserved,
    Published,
}
impl Mode {
    fn label(self) -> &'static str {
        match self {
            Self::Latency { .. } => "latency",
            Self::Reserved => "witness-reserved",
            Self::Published => "witness-published",
        }
    }
    fn stage(self) -> u32 {
        match self {
            Self::Latency { .. } => 0,
            Self::Reserved => 1,
            Self::Published => 2,
        }
    }
    fn counts(self) -> (u32, u32) {
        match self {
            Self::Latency { samples, warmup } => (samples, warmup),
            _ => (0, 0),
        }
    }
}

fn parse() -> Result<Mode> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("latency") if args.len() <= 3 => {
            let samples = match args.get(1) {
                Some(v) => v.parse().map_err(|_| "invalid-samples")?,
                None => 128,
            };
            let warmup = match args.get(2) {
                Some(v) => v.parse().map_err(|_| "invalid-warmup")?,
                None => 16,
            };
            if !(1..=4096).contains(&samples) || warmup > 1024 {
                return Err("count-out-of-bounds");
            }
            Ok(Mode::Latency { samples, warmup })
        }
        Some("witness-reserved") if args.len() == 1 => Ok(Mode::Reserved),
        Some("witness-published") if args.len() == 1 => Ok(Mode::Published),
        _ => Err("usage-latency-samples-warmup-or-witness-reserved-or-witness-published"),
    }
}

fn fatal(reason: &'static str) -> ! {
    // Only failure paths print while a worker could still be live. No success
    // record is emitted; the independent capture deadline must also reject it.
    eprintln!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"fatal\",\"reason\":\"{reason}\",\"success\":false}}");
    std::process::abort()
}

fn wait_until(mut predicate: impl FnMut() -> bool) -> Result<()> {
    let deadline = Instant::now() + Duration::from_millis(WAIT_MS.into());
    while !predicate() {
        if Instant::now() >= deadline {
            return Err("observation-deadline");
        }
        // Lets lower priorities progress. Completion is proved by predicate,
        // never inferred from the duration of this setup-only sleep.
        thread::sleep(Duration::from_millis(1));
    }
    Ok(())
}

fn join_bounded<T>(handle: JoinHandle<Result<T>>) -> Result<T> {
    if wait_until(|| handle.is_finished()).is_err() {
        fatal("worker-join-deadline");
    }
    handle.join().map_err(|_| "worker-panic")?
}

struct Watchdog {
    stop: Arc<Gate>,
    handle: JoinHandle<Result<()>>,
    schedule: Schedule,
}
impl Watchdog {
    fn start() -> Result<Self> {
        let stop = Gate::new()?;
        let ready = Gate::new()?;
        let priority = Arc::new(AtomicI32::new(-1));
        let policy = Arc::new(AtomicI32::new(-1));
        let (stop_worker, ready_worker) = (stop.clone(), ready.clone());
        let (priority_worker, policy_worker) = (priority.clone(), policy.clone());
        let handle = thread::Builder::new()
            .stack_size(8192)
            .spawn(move || {
                let actual = if native::is_nuttx() {
                    native::fifo(native::tid(), WATCHDOG_PRIORITY)
                } else {
                    native::schedule(native::tid())
                };
                if let Ok(value) = actual {
                    policy_worker.store(value.policy, Ordering::Relaxed);
                    priority_worker.store(value.priority, Ordering::Release);
                }
                ready_worker.post()?;
                actual?;
                if stop_worker.wait(RUN_BUDGET_MS).is_err() {
                    fatal("watchdog-deadline-or-native-error");
                }
                Ok(())
            })
            .map_err(|_| "watchdog-spawn")?;
        if ready.wait(WAIT_MS).is_err() {
            fatal("watchdog-setup-deadline");
        }
        let schedule = Schedule {
            priority: priority.load(Ordering::Acquire),
            policy: policy.load(Ordering::Relaxed),
        };
        if schedule.priority < 0 {
            join_bounded(handle)?;
            return Err("watchdog-scheduling");
        }
        Ok(Self {
            stop,
            handle,
            schedule,
        })
    }
    fn finish(self) -> Result<()> {
        if self.stop.post().is_err() {
            fatal("watchdog-stop-failed");
        }
        join_bounded(self.handle)
    }
}

struct Clock {
    hz: u64,
    source: &'static str,
    accessor: &'static str,
    frequency_basis: &'static str,
    native_bits: u32,
    monotonic_resolution_ns: u64,
    probe_ticks: u32,
    probe_ns: u64,
    counter_validated: bool,
}
impl Clock {
    fn new() -> Result<Self> {
        let request = match env!("NXRS_STD_CHANNEL_CLOCK_SOURCE") {
            "baseline" => 0,
            "native-counter" => 1,
            _ => return Err("build-clock-source"),
        };
        let (hz, source, native_bits) = native::counter_info(request)?;
        if hz == 0 {
            return Err("zero-counter-frequency");
        }
        let monotonic_resolution_ns = native::resolution_ns()?;
        // Only a coarse sanity check, not clock accuracy calibration. Native
        // frequency/configuration and physical provenance still need review.
        // Stay active: a CPU cycle counter may stop while an idle task uses
        // WFI. The iteration limit also bounds a broken monotonic clock.
        let before = native::monotonic_ns()?;
        let first = native::counter();
        let mut active_probe_complete = false;
        for _ in 0..2_000_000 {
            if native::monotonic_ns()?.saturating_sub(before) >= 20_000_000 {
                active_probe_complete = true;
                break;
            }
            std::hint::spin_loop();
        }
        if !active_probe_complete {
            return Err("counter-probe-deadline");
        }
        let last = native::counter();
        let after = native::monotonic_ns()?;
        let probe_ns = after.checked_sub(before).ok_or("monotonic-reversed")?;
        let probe_ticks = last.wrapping_sub(first);
        let expected = u128::from(probe_ns) * u128::from(hz) / 1_000_000_000;
        let tolerance =
            expected / 2 + u128::from(monotonic_resolution_ns) * u128::from(hz) / 1_000_000_000;
        let counter_validated = source != 3
            && source != 7
            && probe_ticks > 0
            && probe_ns > 0
            && u128::from(probe_ticks).abs_diff(expected) <= tolerance;
        let (source, accessor, frequency_basis) = match source {
            1 => ("nuttx-arch-perf", "perf_gettime-low32", "native-perf-api"),
            2 => ("nuttx-arch-timer", "perf_gettime-low32", "native-perf-api"),
            3 => (
                "nuttx-system-ticks",
                "perf_gettime-low32",
                "native-perf-api",
            ),
            4 => (
                "linux-monotonic",
                "clock_gettime-monotonic-low32",
                "nanoseconds",
            ),
            5 => (
                "xtensa-ccount",
                "XTHAL_GET_CCOUNT",
                "CONFIG_ESP32S3_DEFAULT_CPU_FREQ_MHZ",
            ),
            6 => (
                "arm-dwt-cyccnt",
                "up_perf_gettime-low32",
                "fixture-up_perf_init-BOARD_SYS_FREQ",
            ),
            7 => (
                "nuttx-monotonic",
                "clock_gettime-monotonic-low32",
                "nanoseconds-functional-fallback",
            ),
            _ => return Err("unknown-counter-source"),
        };
        Ok(Self {
            hz,
            source,
            accessor,
            frequency_basis,
            native_bits,
            monotonic_resolution_ns,
            probe_ticks,
            probe_ns,
            counter_validated,
        })
    }
    fn wrap_upper_ns(&self) -> u64 {
        ((1u128 << 31) * 1_000_000_000 / u128::from(self.hz)) as u64
    }
}

struct Metadata<'a> {
    run_id: &'a str,
    mode: Mode,
    clock: &'a Clock,
    sender: Schedule,
    receiver: Schedule,
    observer: Schedule,
    watchdog: Schedule,
}
impl Metadata<'_> {
    fn print(&self) {
        let (samples, warmup) = self.mode.counts();
        println!(concat!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"metadata\",",
            "\"run_id\":\"{}\",\"build_id\":\"{}\",\"runtime\":\"{}\",\"mode\":\"{}\",\"hook_stage\":{},",
            "\"capacity\":{},\"payload_bytes\":16,\"samples\":{},\"warmup\":{},",
            "\"clock_source\":\"{}\",\"clock_hz\":{},\"counter_bits\":32,\"native_counter_bits\":{},",
            "\"clock_source_request\":\"{}\",\"clock_frequency_basis\":\"{}\",\"counter_accessor\":\"{}\",",
            "\"counter_probe_kind\":\"active-monotonic-plausibility\",\"witness_api\":\"{}\",",
            "\"monotonic_resolution_ns\":{},\"counter_probe_ticks\":{},\"counter_probe_ns\":{},\"counter_validated\":{},",
            "\"sender_policy\":{},\"sender_priority\":{},\"receiver_policy\":{},\"receiver_priority\":{},",
            "\"observer_policy\":{},\"observer_priority\":{},\"watchdog_policy\":{},\"watchdog_priority\":{},",
            "\"physical_measurement\":false,\"single_core_tcb_witness\":{},\"blocked_observation_scope\":\"WAIT_SEM-inside-recv\","
            "\"terminal_quiescence_protocol\":\"{}\"}}"),
            self.run_id, env!("NXRS_STD_CHANNEL_BUILD_ID"), if native::is_nuttx() { "nuttx" } else { "linux-functional" },
            self.mode.label(), self.mode.stage(), CAPACITY, samples, warmup,
            self.clock.source, self.clock.hz, self.clock.native_bits,
            env!("NXRS_STD_CHANNEL_CLOCK_SOURCE"), self.clock.frequency_basis, self.clock.accessor,
            if self.mode.stage() == 0 { "recv" } else { "try_recv" },
            self.clock.monotonic_resolution_ns, self.clock.probe_ticks, self.clock.probe_ns, self.clock.counter_validated,
            self.sender.policy, self.sender.priority, self.receiver.policy, self.receiver.priority,
            self.observer.policy, self.observer.priority, self.watchdog.policy, self.watchdog.priority, native::is_nuttx(),
            if self.mode.stage() == 0 { "final-gate-native-wait-v1" } else { "not-applicable" });
    }
}

fn run() -> Result<()> {
    let mode = parse()?;
    let built_stage: u32 = env!("NXRS_STD_CHANNEL_HOOK_STAGE")
        .parse()
        .map_err(|_| "build-hook-stage")?;
    if mode.stage() != built_stage {
        return Err("mode-does-not-match-private-std-build");
    }
    if mode.stage() != 0 && !native::is_nuttx() {
        return Err("priority-witness-requires-single-core-flat-nuttx");
    }
    let run_id = format!("{}-{}", native::tid(), native::monotonic_ns()?);
    let clock = Clock::new()?;
    let original = native::schedule(native::tid())?;
    let watchdog = Watchdog::start()?;
    let watchdog_schedule = watchdog.schedule;
    let result = match mode {
        Mode::Latency { samples, warmup } => latency::run(samples, warmup, &clock).map(|report| {
            (
                report.sender,
                report.receiver,
                report.observer,
                Report::Latency(report),
            )
        }),
        Mode::Reserved | Mode::Published => witness::run(mode.stage()).map(|report| {
            (
                report.sender,
                report.receiver,
                report.observer,
                Report::Witness(report),
            )
        }),
    };
    let restore = native::set_schedule(native::tid(), original);
    let watchdog_stopped = watchdog.finish();
    restore?;
    watchdog_stopped?;
    let (sender, receiver, observer, report) = result?;
    Metadata {
        run_id: &run_id,
        mode,
        clock: &clock,
        sender,
        receiver,
        observer,
        watchdog: watchdog_schedule,
    }
    .print();
    match report {
        Report::Latency(report) => report.print(&run_id, &clock),
        Report::Witness(report) => report.print(&run_id),
    }
}

enum Report {
    Latency(latency::Report),
    Witness(witness::Report),
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(reason) => {
            eprintln!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"failure\",\"reason\":\"{reason}\",\"success\":false}}");
            std::process::ExitCode::FAILURE
        }
    }
}

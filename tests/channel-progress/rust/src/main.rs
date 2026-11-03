//! Source-pinned Crossbeam progress witnesses. Never a production transport.
//!
//! The shared native module is only mechanical scheduler/semaphore translation.
//! Counter helpers are deliberately unused: this fixture makes no timing claim.
#[allow(dead_code)]
#[path = "../../../std-channel-perf/rust/src/native.rs"]
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
const LOW_PRIORITY: i32 = 120;
const HIGH_PRIORITY: i32 = 180;
const WAKER_PRIORITY: i32 = 160;
const OBSERVER_PRIORITY: i32 = 220;
const WATCHDOG_PRIORITY: i32 = 240;
const WAIT_MS: u32 = 1000;
const RUN_BUDGET_MS: u32 = 15000;

#[derive(Clone, Copy, Debug)]
struct Message([u32; 4]);
impl Message {
    fn new(sequence: u32) -> Self {
        Self([sequence, !sequence, sequence ^ 0x9173_ac5e, 0x1357_2468])
    }
    fn valid(self, sequence: u32) -> bool {
        self.0 == Self::new(sequence).0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Mode {
    Host,
    DirectReserved,
    DirectPublished,
    SelectReserved,
    SelectPublished,
    SendReserved,
    SendReclaimed,
}
impl Mode {
    fn parse(value: &str) -> Result<Self> {
        match value {
            "host-functional" => Ok(Self::Host),
            "direct-reserved" => Ok(Self::DirectReserved),
            "direct-published" => Ok(Self::DirectPublished),
            "select-reserved" => Ok(Self::SelectReserved),
            "select-published" => Ok(Self::SelectPublished),
            "send-reserved" => Ok(Self::SendReserved),
            "send-reclaimed" => Ok(Self::SendReclaimed),
            _ => Err("unknown-progress-case"),
        }
    }
    fn label(self) -> &'static str {
        match self {
            Self::Host => "host-functional",
            Self::DirectReserved => "direct-reserved",
            Self::DirectPublished => "direct-published",
            Self::SelectReserved => "select-reserved",
            Self::SelectPublished => "select-published",
            Self::SendReserved => "send-reserved",
            Self::SendReclaimed => "send-reclaimed",
        }
    }
    fn stage(self) -> u32 {
        match self {
            Self::Host => 0,
            Self::DirectReserved | Self::SelectReserved => 1,
            Self::DirectPublished | Self::SelectPublished => 2,
            Self::SendReserved => 3,
            Self::SendReclaimed => 4,
        }
    }
    fn selected(self) -> bool {
        matches!(self, Self::SelectReserved | Self::SelectPublished)
    }
    fn reverse(self) -> bool {
        matches!(self, Self::SendReserved | Self::SendReclaimed)
    }
    fn sender_priority(self) -> i32 {
        if self.reverse() {
            HIGH_PRIORITY
        } else {
            LOW_PRIORITY
        }
    }
    fn receiver_priority(self) -> i32 {
        if self.reverse() {
            LOW_PRIORITY
        } else {
            HIGH_PRIORITY
        }
    }
    fn witness_api(self) -> &'static str {
        match self {
            Self::Host => "functional-smoke",
            Self::SelectReserved | Self::SelectPublished => "select_biased",
            Self::SendReserved | Self::SendReclaimed => "send",
            _ => "recv",
        }
    }
    fn messages(self) -> usize {
        if self == Self::Host {
            10
        } else if self.reverse() {
            CAPACITY + 1
        } else {
            1
        }
    }
}

fn fatal(reason: &'static str) -> ! {
    eprintln!("CHANNEL_PROGRESS {{\"schema\":\"channel-progress-v1\",\"record\":\"fatal\",\"reason\":\"{reason}\",\"success\":false}}");
    std::process::abort()
}

fn wait_until(mut predicate: impl FnMut() -> bool) -> Result<()> {
    let deadline = Instant::now() + Duration::from_millis(WAIT_MS.into());
    while !predicate() {
        if Instant::now() >= deadline {
            return Err("observation-deadline");
        }
        // Setup/cleanup only. The predicate, never elapsed sleep, proves state.
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
                    fatal("independent-watchdog-deadline");
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

fn metadata(run_id: &str, mode: Mode, report: &witness::Report, watchdog: Schedule) {
    let selected = mode.selected() || mode == Mode::Host;
    println!(concat!("CHANNEL_PROGRESS {{\"schema\":\"channel-progress-v1\",\"record\":\"metadata\",",
        "\"run_id\":\"{}\",\"build_id\":\"{}\",\"mode\":\"{}\",\"hook_stage\":{},",
        "\"runtime\":\"{}\",\"transport\":\"crossbeam-channel\",\"transport_version\":\"0.5.17\",",
        "\"capacity\":{},\"payload_bytes\":16,\"arm_count\":{},\"provider_count\":1,",
        "\"selection_bias\":\"{}\",\"witness_api\":\"{}\",\"single_core_tcb_witness\":{},",
        "\"sender_policy\":{},\"sender_priority\":{},\"receiver_policy\":{},\"receiver_priority\":{},",
        "\"observer_policy\":{},\"observer_priority\":{},\"watchdog_policy\":{},\"watchdog_priority\":{},",
        "\"waker_policy\":{},\"waker_priority\":{}}}"),
        run_id, env!("NXRS_CHANNEL_PROGRESS_BUILD_ID"), mode.label(), mode.stage(),
        if native::is_nuttx() { "nuttx" } else { "linux-functional" },
        CAPACITY, if selected { 3 } else { 1 },
        if selected { "stop-important-ordinary" } else { "none" }, mode.witness_api(), native::is_nuttx(),
        report.sender.policy, report.sender.priority, report.receiver.policy, report.receiver.priority,
        report.observer.policy, report.observer.priority, watchdog.policy, watchdog.priority,
        report.waker.policy, report.waker.priority);
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 1 {
        return Err("usage-channel-progress-one-named-case");
    }
    let mode = Mode::parse(&args[0])?;
    let built_stage: u32 = env!("NXRS_CHANNEL_PROGRESS_HOOK_STAGE")
        .parse()
        .map_err(|_| "build-hook-stage")?;
    if mode.stage() != built_stage {
        return Err("mode-does-not-match-private-crossbeam-build");
    }
    if mode != Mode::Host && !native::is_nuttx() {
        return Err("priority-witness-requires-single-core-flat-nuttx");
    }
    if mode == Mode::Host && native::is_nuttx() {
        return Err("host-functional-case-requires-linux");
    }
    let run_id = format!("{}-{}", native::tid(), native::monotonic_ns()?);
    let original = native::schedule(native::tid())?;
    let watchdog = Watchdog::start()?;
    let watchdog_schedule = watchdog.schedule;
    let result = if mode == Mode::Host {
        witness::host()
    } else {
        witness::run(mode)
    };
    let restore = native::set_schedule(native::tid(), original);
    let watchdog_stopped = watchdog.finish();
    restore?;
    watchdog_stopped?;
    let report = result?;
    metadata(&run_id, mode, &report, watchdog_schedule);
    report.print(&run_id, mode)
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(reason) => {
            eprintln!("CHANNEL_PROGRESS {{\"schema\":\"channel-progress-v1\",\"record\":\"failure\",\"reason\":\"{reason}\",\"success\":false}}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cases_bind_the_real_transition_and_priority_relationship() {
        for (name, stage, sender, receiver, selected) in [
            ("direct-reserved", 1, 120, 180, false),
            ("direct-published", 2, 120, 180, false),
            ("select-reserved", 1, 120, 180, true),
            ("select-published", 2, 120, 180, true),
            ("send-reserved", 3, 180, 120, false),
            ("send-reclaimed", 4, 180, 120, false),
        ] {
            let mode = Mode::parse(name).unwrap();
            assert_eq!(
                (
                    mode.stage(),
                    mode.sender_priority(),
                    mode.receiver_priority(),
                    mode.selected()
                ),
                (stage, sender, receiver, selected)
            );
        }
        assert!(Mode::parse("latency").is_err());
    }
    #[test]
    fn payload_checks_every_word() {
        assert!(Message::new(9).valid(9));
        for index in 0..4 {
            let mut value = Message::new(9);
            value.0[index] ^= 1;
            assert!(!value.valid(9));
        }
    }
}

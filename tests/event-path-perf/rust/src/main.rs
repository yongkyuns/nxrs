//! P2 event-path timing ladder. This executable characterizes operations; it
//! cannot grant physical clock or production progress qualification.
mod controls;
mod fanin;
mod ladder;
#[path = "../../../std-channel-perf/rust/src/native.rs"]
mod native;
mod payload;
mod report;
mod support;
mod transport;

use ladder::AdmissionCall;
use native::Result;
use payload::{Bytes16, Payload};
use report::Report;
use support::{Clock, Config};
use transport::{
    CrossbeamDirect, CrossbeamSelectOne, CrossbeamSelectThree, Messaging, RawStd, Transport,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Family {
    Ready,
    Full,
    Blocked,
    Semaphore,
    Park,
    FixedPeer,
    Fanin,
}
impl Family {
    fn name(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::Full => "full-rejection",
            Self::Blocked => "blocked",
            Self::Semaphore => "semaphore",
            Self::Park => "park",
            Self::FixedPeer => "fixed-peer",
            Self::Fanin => "finite-fanin",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Backend {
    Std,
    Messaging,
    Crossbeam,
    SelectOne,
    SelectThree,
    None,
}
impl Backend {
    fn name(self) -> &'static str {
        match self {
            Self::Std => RawStd::NAME,
            Self::Messaging => Messaging::NAME,
            Self::Crossbeam => CrossbeamDirect::NAME,
            Self::SelectOne => CrossbeamSelectOne::NAME,
            Self::SelectThree => CrossbeamSelectThree::NAME,
            Self::None => "notification-control",
        }
    }
    fn arms(self) -> usize {
        match self {
            Self::SelectOne => CrossbeamSelectOne::ARMS,
            Self::SelectThree => CrossbeamSelectThree::ARMS,
            _ => 0,
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct Case {
    name: &'static str,
    family: Family,
    backend: Backend,
    call: AdmissionCall,
    providers: usize,
}
impl Case {
    fn parse(value: &str) -> Result<Self> {
        use AdmissionCall::{Send, TrySend};
        use Backend::{Crossbeam, Messaging, None, SelectOne, SelectThree, Std};
        use Family::{Blocked, Fanin, FixedPeer, Full, Park, Ready, Semaphore};
        let (name, family, backend, call, providers) = match value {
            "ready-std-try" => ("ready-std-try", Ready, Std, TrySend, 1),
            "ready-messaging-try" => ("ready-messaging-try", Ready, Messaging, TrySend, 1),
            "ready-crossbeam-try" => ("ready-crossbeam-try", Ready, Crossbeam, TrySend, 1),
            "ready-std-send" => ("ready-std-send", Ready, Std, Send, 1),
            "ready-messaging-send" => ("ready-messaging-send", Ready, Messaging, Send, 1),
            "ready-crossbeam-send" => ("ready-crossbeam-send", Ready, Crossbeam, Send, 1),
            "full-std" => ("full-std", Full, Std, TrySend, 1),
            "full-messaging" => ("full-messaging", Full, Messaging, TrySend, 1),
            "full-crossbeam" => ("full-crossbeam", Full, Crossbeam, TrySend, 1),
            "blocked-std-try" => ("blocked-std-try", Blocked, Std, TrySend, 1),
            "blocked-messaging-try" => ("blocked-messaging-try", Blocked, Messaging, TrySend, 1),
            "blocked-crossbeam-try" => ("blocked-crossbeam-try", Blocked, Crossbeam, TrySend, 1),
            "blocked-std-send" => ("blocked-std-send", Blocked, Std, Send, 1),
            "blocked-messaging-send" => ("blocked-messaging-send", Blocked, Messaging, Send, 1),
            "blocked-crossbeam-send" => ("blocked-crossbeam-send", Blocked, Crossbeam, Send, 1),
            "ready-select-one" => ("ready-select-one", Ready, SelectOne, TrySend, 1),
            "ready-select-three" => ("ready-select-three", Ready, SelectThree, TrySend, 1),
            "blocked-select-one" => ("blocked-select-one", Blocked, SelectOne, TrySend, 1),
            "blocked-select-three" => ("blocked-select-three", Blocked, SelectThree, TrySend, 1),
            "semaphore" => ("semaphore", Semaphore, None, TrySend, 1),
            "park" => ("park", Park, None, TrySend, 1),
            "fixed-peer" => ("fixed-peer", FixedPeer, None, TrySend, 1),
            "fanin-std-1" => ("fanin-std-1", Fanin, Std, TrySend, 1),
            "fanin-std-4" => ("fanin-std-4", Fanin, Std, TrySend, 4),
            "fanin-messaging-1" => ("fanin-messaging-1", Fanin, Messaging, TrySend, 1),
            "fanin-messaging-4" => ("fanin-messaging-4", Fanin, Messaging, TrySend, 4),
            "fanin-crossbeam-1" => ("fanin-crossbeam-1", Fanin, Crossbeam, TrySend, 1),
            "fanin-crossbeam-4" => ("fanin-crossbeam-4", Fanin, Crossbeam, TrySend, 4),
            _ => return Err("unknown-case"),
        };
        Ok(Self {
            name,
            family,
            backend,
            call,
            providers,
        })
    }
    fn admission(self) -> &'static str {
        match self.family {
            Family::Semaphore => "publish-and-sem-post",
            Family::Park => "publish-and-unpark",
            Family::FixedPeer => "publish-and-yield-now",
            _ => self.call.name(),
        }
    }
    fn receive_api(self) -> &'static str {
        match self.family {
            Family::Fanin => "recv-timeout-10ms",
            Family::Semaphore => "sem-clockwait",
            Family::Park => "thread-park-predicate",
            Family::FixedPeer => "known-peer-marker",
            Family::Full => "untimed-validation-drain",
            _ => match self.backend {
                Backend::Messaging => "inbox-wait-none",
                Backend::SelectOne => "select-biased-one",
                Backend::SelectThree => "select-biased-three-ordinary",
                _ => "recv",
            },
        }
    }
}

fn run_transport<T: Transport, P: Payload>(
    case: Case,
    config: Config,
    clock: &Clock,
    capacity: usize,
) -> Result<Report> {
    match case.family {
        Family::Ready | Family::Full => ladder::ready::<T, P>(
            config,
            clock,
            capacity,
            case.call,
            case.family == Family::Full,
        ),
        Family::Blocked => ladder::blocked::<T, P>(config, clock, capacity, case.call),
        Family::Fanin => fanin::run::<T, P>(config, clock, capacity, case.providers),
        _ => Err("transport-case-family"),
    }
}

fn run<P: Payload>(case: Case, config: Config, clock: &Clock, capacity: usize) -> Result<Report> {
    match case.backend {
        Backend::Std => run_transport::<RawStd, P>(case, config, clock, capacity),
        Backend::Messaging => run_transport::<Messaging, P>(case, config, clock, capacity),
        Backend::Crossbeam => run_transport::<CrossbeamDirect, P>(case, config, clock, capacity),
        Backend::SelectOne => run_transport::<CrossbeamSelectOne, P>(case, config, clock, capacity),
        Backend::SelectThree => {
            run_transport::<CrossbeamSelectThree, P>(case, config, clock, capacity)
        }
        Backend::None => match case.family {
            Family::Semaphore => controls::semaphore(config, clock),
            Family::Park => controls::park(config, clock),
            Family::FixedPeer => controls::fixed_peer(config, clock),
            _ => Err("control-case-family"),
        },
    }
}

fn print_experiment<P: Payload>(
    run_id: &str,
    case: Case,
    config: Config,
    clock: &Clock,
    capacity: usize,
) {
    let control = case.backend == Backend::None;
    let runtime = if native::is_nuttx() {
        "nuttx-unqualified"
    } else {
        "host-functional-only"
    };
    let payload = if control { "none" } else { P::NAME };
    let bytes = if control { 0 } else { std::mem::size_of::<P>() };
    let alignment = if control {
        0
    } else {
        std::mem::align_of::<P>()
    };
    let capacity = if control { 0 } else { capacity };
    let (stop_capacity, important_capacity) = if case.backend == Backend::SelectThree {
        (1, capacity)
    } else {
        (0, 0)
    };
    println!(concat!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"experiment\",",
        "\"run_id\":\"{}\",\"case\":\"{}\",\"family\":\"{}\",\"transport\":\"{}\",\"admission\":\"{}\",",
        "\"payload\":\"{}\",\"payload_bytes\":{},\"payload_alignment\":{},\"capacity\":{},\"provider_count\":{},",
        "\"selector_arms\":{},\"stop_capacity\":{},\"important_capacity\":{},\"active_arm\":\"ordinary\",",
        "\"samples\":{},\"warmup\":{},\"runtime_kind\":\"{}\",\"build_id\":\"{}\",",
        "\"clock_hz\":{},\"clock_source\":{},\"counter_native_bits\":{},\"monotonic_resolution_ns\":{},",
        "\"counter_bits\":32,\"clock_source_request\":\"{}\",\"receive_api\":\"{}\",",
        "\"crossbeam_version\":\"0.5.17\",\"witness_hooks\":false,\"worker_stack\":{},\"blocked_state_name\":\"{}\",",
        "\"release_schedule\":\"finite-closed-loop-or-unscheduled-fanin\",\"counts_include_warmup\":true}}"),
        run_id, case.name, case.family.name(), case.backend.name(), case.admission(), payload, bytes, alignment,
        capacity, case.providers, case.backend.arms(), stop_capacity, important_capacity,
        config.samples, config.warmup, runtime, env!("NXRS_EVENT_PATH_BUILD_ID"), clock.hz, clock.source, clock.bits,
        clock.resolution_ns, env!("NXRS_STD_CHANNEL_CLOCK_SOURCE"), case.receive_api(), support::WORKER_STACK, native::State::WaitSem.label());
}

fn print_report(run_id: &str, report: &Report) {
    for (index, (tid, schedule)) in report.provider_schedules.iter().enumerate() {
        println!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"provider\",\"run_id\":\"{run_id}\",\"provider\":{index},\"tid\":{tid},\"policy\":{},\"priority\":{}}}", schedule.policy, schedule.priority);
    }
    println!(concat!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"scheduling\",\"run_id\":\"{}\",",
        "\"sender_tid\":{},\"receiver_tid\":{},\"sender_policy\":{},\"sender_priority\":{},\"receiver_policy\":{},\"receiver_priority\":{},",
        "\"single_core_verified\":{},\"peer_ready_verified\":{}}}"), run_id, report.sender_tid, report.receiver_tid,
        report.sender.policy, report.sender.priority, report.receiver.policy, report.receiver.priority,
        native::is_nuttx(), report.peer_ready_verified);
    for (index, sample) in report.samples.iter().enumerate() {
        let receive_start = sample
            .receive_start
            .map(|v| v.to_string())
            .unwrap_or_else(|| "null".into());
        println!(concat!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"sample\",\"run_id\":\"{}\",",
            "\"index\":{},\"sequence\":{},\"provider\":{},\"start_ticks\":{},\"admission_return_ticks\":{},",
            "\"receive_start_ticks\":{},\"receive_return_ticks\":{},\"elapsed_upper_ns\":{},\"blocked_verified\":{},",
            "\"peer_tid\":{},\"park_returns\":{},\"accepted\":{},\"payload_valid\":true}}"), run_id, index,
            sample.sequence, sample.provider, sample.start, sample.admission_end, receive_start, sample.receive, sample.window_ns,
            sample.blocked, sample.peer_tid, sample.park_returns, sample.accepted);
    }
    println!(concat!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"result\",\"run_id\":\"{}\",",
        "\"success\":true,\"sample_count\":{},\"attempted\":{},\"admitted\":{},\"rejected\":{},\"handled\":{},\"checksum\":{},\"retained_checksum\":{},",
        "\"park_returns\":{},\"threads_joined\":true,\"terminal_quiescence_verified\":{},",
        "\"structural_valid\":true,\"timing_qualified\":false,\"priority_progress_qualified\":false,",
        "\"physical_execution_verified\":false,\"clock_qualified\":false,\"actual_switch_count\":null}}"),
        run_id, report.samples.len(), report.counts.attempted, report.counts.admitted, report.counts.rejected,
        report.counts.handled, report.checksum, report.retained_checksum, report.park_returns, report.terminal_quiescence_verified);
}

pub fn fatal(reason: &'static str) -> ! {
    eprintln!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"fatal\",\"reason\":\"{reason}\",\"success\":false}}");
    std::process::abort()
}

fn execute<P: Payload>(case: Case, config: Config, capacity: usize) -> Result<()> {
    let clock = Clock::new()?;
    let run_id = format!(
        "{}-{}-{}",
        env!("NXRS_EVENT_PATH_BUILD_ID"),
        case.name,
        native::monotonic_ns()?
    );
    print_experiment::<P>(&run_id, case, config, &clock, capacity);
    let original = native::schedule(native::tid())?;
    let watchdog = support::Watchdog::start()?;
    let measured = run::<P>(case, config, &clock, capacity);
    let restored = native::set_schedule(native::tid(), original);
    let watchdog_joined = watchdog.finish();
    restored?;
    watchdog_joined?;
    let report = measured?;
    print_report(&run_id, &report);
    Ok(())
}

fn main() {
    let result = (|| {
        let args: Vec<_> = std::env::args().skip(1).collect();
        if args.is_empty() || args.len() > 4 {
            return Err("usage-case-samples-warmup-bytes16-or-imu16");
        }
        let case = Case::parse(&args[0])?;
        let samples = args
            .get(1)
            .map(|v| v.parse().map_err(|_| "invalid-samples"))
            .transpose()?
            .unwrap_or(128);
        let warmup = args
            .get(2)
            .map(|v| v.parse().map_err(|_| "invalid-warmup"))
            .transpose()?
            .unwrap_or(16);
        let config = Config::new(samples, warmup)?;
        match args.get(3).map(String::as_str).unwrap_or("bytes16") {
            "bytes16" => execute::<Bytes16>(case, config, 8),
            "imu16" if case.backend != Backend::None => execute::<imu::ImuSample>(case, config, 16),
            _ => Err("invalid-payload-profile"),
        }
    })();
    if let Err(reason) = result {
        eprintln!("EVENT_PATH_PERF {{\"schema\":\"event-path-perf-v1\",\"record\":\"failure\",\"reason\":\"{reason}\",\"success\":false}}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_named_cases_keep_admission_and_topology_separate() {
        assert_eq!(
            Case::parse("ready-std-send").unwrap().call,
            AdmissionCall::Send
        );
        assert_eq!(
            Case::parse("ready-messaging-try").unwrap().call,
            AdmissionCall::TrySend
        );
        assert_eq!(Case::parse("ready-select-one").unwrap().backend.arms(), 1);
        assert_eq!(
            Case::parse("blocked-select-three").unwrap().backend.arms(),
            3
        );
        assert_eq!(Case::parse("fanin-std-4").unwrap().providers, 4);
        assert!(Case::parse("yield-alone").is_err());
        assert!(Case::parse("context-switch").is_err());
    }
}

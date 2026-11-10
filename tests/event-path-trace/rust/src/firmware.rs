#[allow(dead_code)]
#[path = "../../../std-channel-perf/rust/src/native.rs"]
mod native;
#[allow(dead_code)]
#[path = "../../../event-path-perf/rust/src/payload.rs"]
mod payload;
mod recorder;
#[allow(dead_code)]
#[path = "../../../event-path-perf/rust/src/support.rs"]
mod support;
#[allow(dead_code)]
#[path = "../../../event-path-perf/rust/src/transport.rs"]
mod transport;

use native::{Gate, Result, Schedule};
use payload::{Bytes16, Payload};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        Arc,
    },
    thread,
};
use transport::{ReceiveEndpoint, SendEndpoint, Transport};

#[global_allocator]
static ALLOCATOR: recorder::TrackingAllocator = recorder::TrackingAllocator;

struct Shared {
    tid: AtomicI32,
    announced: AtomicU32,
    completed: AtomicU32,
    received: AtomicU32,
    valid: AtomicBool,
    cancelled: AtomicBool,
    terminal: AtomicBool,
}

struct Sample {
    sequence: u32,
    start: u32,
    returned: u32,
    received: u32,
    window_ns: u64,
}

fn run_transport<T: Transport>(samples: u32, warmup: u32, equal: bool, record: bool) -> Result<()> {
    let clock = support::Clock::new()?;
    let instrumented = recorder::register()?;
    if record && !instrumented {
        return Err("record-requires-instrumented-image");
    }
    let original = native::schedule(native::tid())?;
    let sender_priority = if equal { 180 } else { 120 };
    let sender_schedule = native::fifo(native::tid(), sender_priority)?;
    let sender_tid = native::tid();
    let sender_stack = recorder::stack(sender_tid)?;
    let watchdog = support::Watchdog::start()?;
    let (tx, mut rx) = recorder::constructor(|| T::channel::<Bytes16>(8));
    let allocations = recorder::allocation_snapshot()?;
    let mut retained = Vec::with_capacity(samples as usize);
    let shared = Arc::new(Shared {
        tid: AtomicI32::new(-1),
        announced: AtomicU32::new(0),
        completed: AtomicU32::new(0),
        received: AtomicU32::new(0),
        valid: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        terminal: AtomicBool::new(false),
    });
    let ready = Gate::new()?;
    let arm = Gate::new()?;
    let done = Gate::new()?;
    let terminal = Gate::new()?;
    let (worker, wr, wa, wd, wt) = (
        shared.clone(),
        ready.clone(),
        arm.clone(),
        done.clone(),
        terminal.clone(),
    );
    let handle = thread::Builder::new()
        .stack_size(support::WORKER_STACK)
        .spawn(move || {
            let schedule = native::fifo(native::tid(), 180);
            if schedule.is_ok() {
                worker.tid.store(native::tid(), Ordering::Release);
            }
            wr.post()?;
            schedule?;
            for sequence in 1..=samples + warmup {
                wa.wait(support::WAIT_MS)?;
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                worker.announced.store(sequence, Ordering::Release);
                recorder::record(12, native::tid(), sequence);
                let value = match rx.recv() {
                    Ok(value) => value,
                    Err(_) if worker.cancelled.load(Ordering::Acquire) => return Ok(()),
                    Err(error) => return Err(error),
                };
                let received = native::counter();
                recorder::record(13, native::tid(), sequence);
                worker.received.store(received, Ordering::Relaxed);
                worker
                    .valid
                    .store(value.valid(sequence, 0), Ordering::Relaxed);
                worker.completed.store(sequence, Ordering::Release);
                wd.post()?;
            }
            worker.terminal.store(true, Ordering::Release);
            wt.wait(support::WAIT_MS)?;
            Ok(())
        })
        .map_err(|_| "receiver-spawn")?;
    let mut receiver_tid = -1;
    let mut receiver_stack = (0, 0);
    let mut checksum = 0_u64;
    let mut window_start = 0;
    let measured = (|| {
        ready.wait(support::WAIT_MS)?;
        receiver_tid = shared.tid.load(Ordering::Acquire);
        if receiver_tid < 1 || receiver_tid == sender_tid {
            return Err("receiver-identity");
        }
        receiver_stack = recorder::stack(receiver_tid)?;
        let receiver_schedule = native::schedule(receiver_tid)?;
        if receiver_schedule.policy != 1 || receiver_schedule.priority != 180 {
            return Err("receiver-schedule");
        }
        for sequence in 1..=samples + warmup {
            if sequence == warmup + 1 {
                window_start = native::monotonic_ns()?;
                if record {
                    recorder::enable(sender_tid, receiver_tid);
                }
            }
            recorder::record(16, sender_tid, sequence);
            arm.post()?;
            support::wait_until(|| shared.announced.load(Ordering::Acquire) == sequence)?;
            support::blocked(receiver_tid, receiver_schedule, || {
                shared.announced.load(Ordering::Acquire) == sequence
                    && shared.completed.load(Ordering::Acquire) < sequence
            })?;
            recorder::record(14, sender_tid, sequence);
            let value = Bytes16::new(sequence, 0);
            let wall = native::monotonic_ns()?;
            let start = native::counter();
            recorder::record(10, sender_tid, sequence);
            let admitted = tx.try_send(value);
            let returned = native::counter();
            recorder::record(11, sender_tid, sequence);
            admitted.map_err(|_| "unexpected-admission-failure")?;
            done.wait(support::WAIT_MS)?;
            if shared.completed.load(Ordering::Acquire) != sequence
                || !shared.valid.load(Ordering::Relaxed)
            {
                return Err("payload-or-sequence");
            }
            let received = shared.received.load(Ordering::Relaxed);
            let upper = clock.window(
                wall,
                native::monotonic_ns()?,
                &[returned.wrapping_sub(start), received.wrapping_sub(start)],
            )?;
            checksum = checksum.wrapping_add(value.checksum());
            recorder::record(15, sender_tid, sequence);
            if sequence > warmup {
                retained.push(Sample {
                    sequence,
                    start,
                    returned,
                    received,
                    window_ns: upper,
                });
            }
        }
        support::wait_until(|| shared.terminal.load(Ordering::Acquire))?;
        support::blocked(
            receiver_tid,
            Schedule {
                policy: 1,
                priority: 180,
            },
            || shared.terminal.load(Ordering::Acquire),
        )?;
        Ok(())
    })();
    // Disabled SDK callbacks never borrow storage while it is serialized.
    recorder::disable();
    let window_end = native::monotonic_ns();
    shared.cancelled.store(true, Ordering::Release);
    drop(tx);
    let release_terminal = terminal.post();
    let release_arm = arm.post();
    let joined = support::join_bounded(handle);
    let stopped = watchdog.finish();
    let restored = native::set_schedule(sender_tid, original);
    measured?;
    release_terminal?;
    release_arm?;
    joined?;
    stopped?;
    restored?;
    let events = recorder::snapshot()?;
    let window_ns = clock.window(window_start, window_end?, &[])?;
    let (buffer_address, buffer_bytes) = recorder::buffer_location();
    println!("EVENT_TRACE {{\"record\":\"metadata\",\"schema\":\"event-path-trace-v1\",\"build_id\":\"{}\",\"transport\":\"{}\",\"samples\":{},\"warmup\":{},\"instrumented\":{},\"recording\":{},\"sender_tid\":{},\"receiver_tid\":{},\"sender_priority\":{},\"receiver_priority\":180,\"policy\":1,\"clock_hz\":{},\"counter_bits\":32,\"window_ns\":{},\"monotonic_resolution_ns\":{},\"trace_capacity\":{},\"trace_bytes\":{},\"trace_address\":{},\"trace_region\":{},\"counter_source\":{}}}",
        env!("NXRS_EVENT_TRACE_BUILD_ID"), T::NAME, samples, warmup, instrumented, record,
        sender_tid, receiver_tid, sender_schedule.priority, clock.hz, window_ns, clock.resolution_ns,
        recorder::CAPACITY, buffer_bytes, buffer_address, recorder::region(buffer_address, buffer_bytes), clock.source);
    for (role, (base, bytes)) in [
        ("sender-stack", sender_stack),
        ("receiver-stack", receiver_stack),
    ] {
        println!("EVENT_TRACE {{\"record\":\"placement\",\"role\":\"{}\",\"address\":{},\"bytes\":{},\"region\":{}}}", role, base, bytes, recorder::region(base, bytes));
    }
    for allocation in &allocations {
        println!("EVENT_TRACE {{\"record\":\"allocation\",\"scope\":\"channel-constructor\",\"address\":{},\"bytes\":{},\"alignment\":{},\"released_during_constructor\":{},\"region\":{}}}",
            allocation.address, allocation.bytes, allocation.alignment, allocation.released,
            recorder::region(allocation.address, allocation.bytes));
    }
    for sample in &retained {
        println!("EVENT_TRACE {{\"record\":\"sample\",\"sequence\":{},\"start\":{},\"admission_return\":{},\"receive_return\":{},\"window_ns\":{},\"blocked_verified\":true}}",
            sample.sequence, sample.start, sample.returned, sample.received, sample.window_ns);
    }
    for (index, event) in events.iter().enumerate() {
        println!("EVENT_TRACE {{\"record\":\"event\",\"index\":{},\"ticks\":{},\"kind\":{},\"tid\":{},\"detail\":{}}}", index, event.ticks, event.kind, event.tid, event.detail);
    }
    println!("EVENT_TRACE {{\"record\":\"result\",\"success\":true,\"sample_count\":{},\"attempted\":{},\"handled\":{},\"checksum\":{},\"event_count\":{},\"overflow\":false,\"terminal_quiescence_verified\":true,\"threads_joined\":true,\"watchdog_joined\":true,\"schedule_restored\":true,\"physical_timing_qualified\":false,\"priority_progress_qualified\":false}}",
        retained.len(), samples + warmup, samples + warmup, checksum, events.len());
    Ok(())
}

fn run() -> Result<()> {
    static STARTED: AtomicBool = AtomicBool::new(false);
    if STARTED.swap(true, Ordering::Relaxed) {
        return Err("fresh-boot-required");
    }
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 6 {
        return Err("usage-backend-unequal-or-equal-quiet-or-record-samples-warmup");
    }
    let equal = match args[2].as_str() {
        "equal" => true,
        "unequal" => false,
        _ => return Err("schedule-case"),
    };
    let record = match args[3].as_str() {
        "record" => true,
        "quiet" => false,
        _ => return Err("record-case"),
    };
    let samples: u32 = args[4].parse().map_err(|_| "samples")?;
    let warmup: u32 = args[5].parse().map_err(|_| "warmup")?;
    if !(1..=128).contains(&samples) || warmup > 64 {
        return Err("bounded-sample-count");
    }
    match args[1].as_str() {
        "std" => run_transport::<transport::RawStd>(samples, warmup, equal, record),
        "messaging" => run_transport::<transport::Messaging>(samples, warmup, equal, record),
        "crossbeam" => run_transport::<transport::CrossbeamDirect>(samples, warmup, equal, record),
        _ => Err("transport-case"),
    }
}

fn main() {
    if let Err(error) = run() {
        fatal(error);
    }
}

fn fatal(error: &str) -> ! {
    eprintln!("EVENT_TRACE_FAILURE {error}");
    std::process::abort()
}

//! One measurement engine, compiled from the same Rust source for both OSes.
use crate::{arrivals, check, irq_probe, line, platform as p, transport::Channel};
use core::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering::*};

pub const MAX: usize = 2240;
const WARM: usize = 128;
const READY: usize = 5;
const PRODUCED: usize = 6;
const FINISHED: usize = 7;
const WARMED: usize = 8;
const RECEIVE: usize = 9;
const RETIRE: usize = 10;
const PROBE: usize = 15;
// The native ABI uses MAX internally for unavailable observations. Never emit
// that sentinel as a measured duration in the wire records.
struct OptionalCounter(u64);
impl core::fmt::Display for OptionalCounter {
    fn fmt(&self, out: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        if self.0 == u64::MAX {
            out.write_str("null")
        } else {
            core::fmt::Display::fmt(&self.0, out)
        }
    }
}

fn cpu_counter_control() -> Option<(u32, u64)> {
    if env!("MPSC_OBSERVER") != "cpu" {
        return None;
    }
    // A short busy interval checks availability and gross unit errors against
    // the independent wall counter. It precedes warmup and workload accounting.
    let before = p::runtime(p::tid());
    let start = p::clock();
    while p::clock().wrapping_sub(start) < p::CLOCK_HZ / 50 {
        core::hint::spin_loop();
    }
    let ticks = p::clock().wrapping_sub(start);
    let after = p::runtime(p::tid());
    check(
        before != u64::MAX && after != u64::MAX && after >= before,
        "cpu-counter-available",
    );
    let cpu_ns = after - before;
    let wall_ns = u64::from(ticks) * 1_000_000_000 / u64::from(p::CLOCK_HZ);
    check(
        wall_ns <= 500_000_000 && cpu_ns >= wall_ns / 2 && cpu_ns <= wall_ns * 3 / 2,
        "cpu-counter-units",
    );
    Some((ticks, cpu_ns))
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Sustained,
    Stream,
    Paced,
    Burst,
    Overload,
    Backpressure,
}
impl Mode {
    pub fn parse(s: &str) -> Self {
        match s {
            "sustained" => Self::Sustained,
            "stream" => Self::Stream,
            "paced" => Self::Paced,
            "burst" => Self::Burst,
            "overload" => Self::Overload,
            "backpressure" => Self::Backpressure,
            _ => {
                check(false, "mode");
                Self::Stream
            }
        }
    }
    fn label(self) -> &'static str {
        match self {
            Self::Sustained => "sustained",
            Self::Stream => "stream",
            Self::Paced => "paced",
            Self::Burst => "burst",
            Self::Overload => "overload",
            Self::Backpressure => "backpressure",
        }
    }
    fn blocking(self) -> bool {
        matches!(self, Self::Sustained | Self::Stream | Self::Backpressure)
    }
}
struct Stamp {
    planned: AtomicU32,
    offered: AtomicU32,
    returned: AtomicU32,
    received: AtomicU32,
    finished: AtomicU32,
    order: AtomicU32,
    flags: AtomicU32,
}
impl Stamp {
    const fn new() -> Self {
        Self {
            planned: AtomicU32::new(0),
            offered: AtomicU32::new(0),
            returned: AtomicU32::new(0),
            received: AtomicU32::new(0),
            finished: AtomicU32::new(0),
            order: AtomicU32::new(0),
            flags: AtomicU32::new(0),
        }
    }
}
struct Role {
    tid: AtomicUsize,
    start_priority: AtomicU32,
    end_priority: AtomicU32,
    start_policy: AtomicU32,
    end_policy: AtomicU32,
}
impl Role {
    const fn new() -> Self {
        Self {
            tid: AtomicUsize::new(0),
            start_priority: AtomicU32::new(0),
            end_priority: AtomicU32::new(0),
            start_policy: AtomicU32::new(0),
            end_policy: AtomicU32::new(0),
        }
    }
}
struct Shared {
    stamps: [Stamp; MAX],
    roles: [Role; 6],
    order: AtomicU32,
    receiver_done: AtomicBool,
    receiving: AtomicBool,
    last: AtomicU32,
    sustained_checksum: AtomicU32,
    sustained_counts: [AtomicU32; 4],
}
static SHARED: Shared = Shared {
    stamps: [const { Stamp::new() }; MAX],
    roles: [const { Role::new() }; 6],
    order: AtomicU32::new(0),
    receiver_done: AtomicBool::new(false),
    receiving: AtomicBool::new(false),
    last: AtomicU32::new(0),
    sustained_checksum: AtomicU32::new(0),
    sustained_counts: [const { AtomicU32::new(0) }; 4],
};

struct Context<C, const W: usize> {
    channel: C,
    mode: Mode,
    producers: usize,
    samples: usize,
    capacity: usize,
}
fn enter(role: usize) {
    let priority = p::configure();
    SHARED.roles[role].tid.store(p::tid(), Release);
    SHARED.roles[role].start_priority.store(priority, Release);
    SHARED.roles[role].start_policy.store(p::policy(), Release);
}
fn prove_senders<C: Channel<W>, const W: usize>(producers: usize) {
    for _ in 0..producers {
        p::gate_wait(PROBE);
    }
    let guard = p::monotonic_ns();
    loop {
        if (2..=producers + 1).all(|role| C::waiting(SHARED.roles[role].tid.load(Acquire), true)) {
            return;
        }
        check(
            p::monotonic_ns() - guard < 1_000_000_000,
            "blocked-senders-proof",
        );
        p::sleep_ns(1_000_000);
    }
}
fn prove_receiver<C: Channel<W>, const W: usize>() {
    let guard = p::monotonic_ns();
    while !(SHARED.receiving.load(Acquire) && C::waiting(SHARED.roles[1].tid.load(Acquire), false))
    {
        check(
            p::monotonic_ns() - guard < 1_000_000_000,
            "empty-receiver-proof",
        );
        p::sleep_ns(1_000_000);
    }
}
fn payload<const W: usize>(id: usize, stamp: u32) -> [u32; W] {
    core::array::from_fn(|i| match i {
        0 => id as u32,
        1 => stamp,
        _ => {
            (id as u32).rotate_left(i as u32)
                ^ stamp.rotate_right(i as u32)
                ^ 0x9173ac5e_u32.wrapping_mul(i as u32)
        }
    })
}
fn offer<C: Channel<W>, const W: usize>(
    ctx: &Context<C, W>,
    role: usize,
    id: usize,
    planned: Option<u32>,
    blocking: bool,
) {
    let mut value = payload::<W>(id, 0);
    let start = p::clock();
    value[1] = start;
    // Timestamp is not part of the synthetic check words: construction remains
    // outside the send interval and receiver validation uses the same convention.
    let s = &SHARED.stamps[id];
    s.planned.store(planned.unwrap_or(start), Relaxed);
    s.offered.store(start, Relaxed);
    check(s.flags.swap(1, Release) == 0, "duplicate-offer");
    let result = ctx.channel.send(role, value, blocking);
    s.returned.store(p::clock(), Relaxed);
    if let Err(returned) = result {
        check(
            core::hint::black_box(returned) == core::hint::black_box(value),
            "rejected-value",
        );
    }
    s.flags
        .fetch_or(if result.is_ok() { 2 } else { 4 }, Release);
}
extern "C" fn worker<C: Channel<W>, const W: usize>(address: usize, role: usize) {
    let ctx = unsafe { &*(address as *const Context<C, W>) };
    enter(role);
    p::gate_post(READY);
    p::gate_wait(role - 1);
    if role == 1 {
        let mut count = 0;
        let mut sustained_count = 0usize;
        let mut sustained_checksum = 0u32;
        let mut next_sequence = [0usize; 4];
        loop {
            SHARED.receiving.store(true, Release);
            // This role is the unique receiver, joined before channel drop.
            let value = unsafe { ctx.channel.receive() };
            let received = if ctx.mode == Mode::Sustained && value[0] >= WARM as u32 {
                0
            } else {
                p::clock()
            };
            SHARED.receiving.store(false, Release);
            let id = value[0] as usize;
            if id == u32::MAX as usize {
                break;
            }
            if id == u32::MAX as usize - 1 {
                p::gate_post(WARMED);
                p::gate_wait(RECEIVE);
                continue;
            }
            let mut expected = payload::<W>(id, 0);
            expected[1] = value[1];
            check(value == expected, "payload-corruption");
            if ctx.mode == Mode::Sustained && id >= WARM {
                let index = id - WARM;
                check(index < ctx.samples && value[1] == 0, "sustained-payload");
                let producer = index % ctx.producers;
                check(
                    index / ctx.producers == next_sequence[producer],
                    "sustained-FIFO",
                );
                next_sequence[producer] += 1;
                sustained_count += 1;
                sustained_checksum = sustained_checksum.wrapping_add(id as u32);
                if sustained_count == ctx.samples {
                    SHARED.last.store(p::clock(), Release);
                }
                continue;
            }
            check(id < MAX, "payload-id");
            let s = &SHARED.stamps[id];
            check(s.offered.load(Acquire) == value[1], "payload-timestamp");
            s.received.store(received, Relaxed);
            s.finished.store(p::clock(), Relaxed);
            s.order.store(SHARED.order.fetch_add(1, Relaxed), Relaxed);
            check(s.flags.fetch_or(8, Release) & 8 == 0, "duplicate-receive");
            count += 1;
            if id >= WARM && id < WARM + ctx.samples {
                SHARED.last.store(received, Release);
            }
            if count == WARM {
                p::gate_post(WARMED);
                p::gate_wait(RECEIVE);
            }
        }
        if ctx.mode == Mode::Sustained {
            check(sustained_count == ctx.samples, "sustained-count");
            SHARED.sustained_checksum.store(sustained_checksum, Release);
            for (producer, value) in next_sequence.iter().enumerate().take(ctx.producers) {
                SHARED.sustained_counts[producer].store(*value as u32, Release);
            }
        }
        SHARED.receiver_done.store(true, Release);
        p::gate_post(FINISHED);
    } else {
        let producer = role - 2;
        p::gate_post(PROBE);
        for id in (ctx.capacity + producer..WARM).step_by(ctx.producers) {
            offer(ctx, role, id, None, true);
        }
        p::gate_post(PRODUCED);
        p::gate_wait(role - 1);
        for index in (producer..ctx.samples).step_by(ctx.producers) {
            if ctx.mode == Mode::Sustained {
                check(
                    ctx.channel
                        .send(role, payload::<W>(WARM + index, 0), true)
                        .is_ok(),
                    "sustained-admission",
                );
                continue;
            }
            let planned = arrivals::release(ctx.mode, index, ctx.producers);
            if ctx.mode == Mode::Backpressure && index == producer {
                p::gate_post(PROBE);
            }
            offer(ctx, role, WARM + index, planned, ctx.mode.blocking());
        }
        p::gate_post(PRODUCED);
    }
    // The controller closes timing/counters before releasing RETIRE. Endpoint
    // diagnostics must not delay delivery of a producer's final message.
    p::gate_wait(RETIRE + role - 1);
    let end_priority = p::priority();
    if role > 1 {
        check(
            end_priority == SHARED.roles[role].start_priority.load(Acquire),
            "producer-priority",
        );
    }
    SHARED.roles[role].end_priority.store(end_priority, Release);
    SHARED.roles[role].end_policy.store(p::policy(), Release);
}
pub fn run<C: Channel<W>, const W: usize>(
    mode: Mode,
    producers: usize,
    capacity: usize,
    samples: usize,
) {
    check(mode != Mode::Paced || samples <= 256, "paced-window-bound");
    let original = p::schedule();
    enter(0);
    for i in 0..20 {
        p::gate_init(i);
    }
    p::watchdog_start(60_000);
    let cpu_control = cpu_counter_control();
    let before = p::heap();
    irq_probe::prepare();
    let ctx = Context::<C, W> {
        channel: C::create(capacity, producers),
        mode,
        producers,
        samples,
        capacity,
    };
    let mut threads = p::Threads::new();
    // Borrowed context remains live until every callback has joined.
    let address = &ctx as *const Context<C, W> as usize;
    for role in 1..=producers + 1 {
        threads.spawn(worker::<C, W>, address, role);
    }
    for _ in 0..producers + 1 {
        p::gate_wait(READY);
    }
    for id in 0..capacity {
        offer(&ctx, 0, id, None, true);
    }
    for role in 2..=producers + 1 {
        p::gate_post(role - 1);
    }
    prove_senders::<C, W>(producers);
    p::gate_post(0);
    for _ in 0..producers {
        p::gate_wait(PRODUCED);
    }
    p::gate_wait(WARMED);
    p::gate_post(RECEIVE);
    prove_receiver::<C, W>();
    irq_probe::start_idle();
    let idle_begin = p::clock();
    let owner_idle_before = p::runtime(SHARED.roles[1].tid.load(Acquire));
    p::sleep_ns(100_000_000);
    let idle_ticks = p::clock().wrapping_sub(idle_begin);
    let idle_end = p::runtime(SHARED.roles[1].tid.load(Acquire));
    irq_probe::finish_idle();
    let owner_idle_ns = if idle_end == u64::MAX || owner_idle_before == u64::MAX {
        None
    } else {
        idle_end.checked_sub(owner_idle_before)
    };
    let mut prefix = 0;
    if mode == Mode::Backpressure {
        check(
            ctx.channel
                .send(0, payload::<W>(u32::MAX as usize - 1, 0), true)
                .is_ok(),
            "receiver-hold",
        );
        p::gate_wait(WARMED);
        for id in WARM + samples..WARM + samples + capacity {
            offer(&ctx, 0, id, None, true);
            prefix += 1;
        }
        check(
            ctx.channel
                .send(0, payload::<W>(u32::MAX as usize - 1, 0), false)
                .is_err(),
            "full-probe",
        );
    }
    arrivals::prepare(mode, producers, samples);
    let mut cpu_before = [u64::MAX; 6];
    // Exclude idle observation from the busy CPU interval.
    for (role, value) in cpu_before.iter_mut().enumerate().take(producers + 2) {
        *value = p::runtime(SHARED.roles[role].tid.load(Acquire));
        check(
            (*value != u64::MAX) == (env!("MPSC_OBSERVER") == "cpu"),
            "cpu-observer-config",
        );
    }
    irq_probe::start();
    let begin = p::clock();
    let wall_begin = p::monotonic_ns();
    for role in 2..=producers + 1 {
        p::gate_post(role - 1);
    }
    arrivals::start();
    let mut blocked = 0;
    let mut recovery = begin;
    if mode == Mode::Backpressure {
        prove_senders::<C, W>(producers);
        blocked = producers;
        recovery = p::clock();
        p::gate_post(RECEIVE);
    }
    for _ in 0..producers {
        p::gate_wait(PRODUCED);
    }
    check(
        ctx.channel
            .send(0, payload::<W>(u32::MAX as usize, 0), true)
            .is_ok(),
        "terminal-send",
    );
    p::gate_wait(FINISHED);
    let end = p::clock();
    let wall_ns = p::monotonic_ns() - wall_begin;
    irq_probe::finish();
    check(
        wall_ns < 8_000_000_000 && end.wrapping_sub(begin) < 1 << 31,
        "counter-window",
    );
    let source_groups = arrivals::finish();
    let mut cpu = [u64::MAX; 6];
    let mut stacks = [[0u32; 3]; 6];
    for role in 0..producers + 2 {
        let tid = SHARED.roles[role].tid.load(Acquire);
        let cpu_end = p::runtime(tid);
        cpu[role] = if cpu_end == u64::MAX || cpu_before[role] == u64::MAX {
            u64::MAX
        } else {
            cpu_end.checked_sub(cpu_before[role]).unwrap_or(u64::MAX)
        };
        stacks[role] = p::stack(tid);
    }
    SHARED.roles[0].end_priority.store(p::priority(), Release);
    SHARED.roles[0].end_policy.store(p::policy(), Release);
    let live = p::heap();
    let join_start = p::clock();
    for role in 1..=producers + 1 {
        p::gate_post(RETIRE + role - 1);
    }
    threads.join();
    check(SHARED.receiver_done.load(Acquire), "receiver-not-joined");
    let backend = C::NAME;
    drop(ctx);
    let join_ticks = p::clock().wrapping_sub(join_start);
    let after = p::heap();
    for i in 0..20 {
        p::gate_destroy(i);
    }
    p::watchdog_stop();
    p::restore(original);
    line(format_args!(
        "MPSC {{\"record\":\"metadata\",\"schema\":\"mpsc-comparison-v4\",\"build_id\":\"{}\",\"runtime\":\"{}\",\"backend\":\"{}\",\"observer\":\"{}\",\"cpu_scope\":\"{}\",\"wait_observer\":\"{}\",\"mode\":\"{}\",\"producers\":{},\"payload_bytes\":{},\"capacity\":{},\"samples\":{},\"warmup\":128,\"warm_blocked_senders\":{},\"prefix\":{},\"clock_hz\":{},\"begin\":{},\"end\":{},\"last_received\":{},\"wall_ns\":{},\"blocked_senders\":{},\"recovery_begin\":{},\"joined_ticks\":{},\"idle_ticks\":{},\"idle_owner_ns\":{},\"source_groups\":{},\"sample_storage_bytes\":{},\"native\":{}}}",
        env!("MPSC_BUILD_ID"),
        p::NAME,
        backend,
        env!("MPSC_OBSERVER"),
        env!("MPSC_CPU_SCOPE"),
        C::WAIT_OBSERVER,
        mode.label(),
        producers,
        W * 4,
        capacity,
        samples,
        producers,
        prefix,
        p::CLOCK_HZ,
        begin,
        end,
        SHARED.last.load(Acquire),
        wall_ns,
        blocked,
        recovery,
        join_ticks,
        idle_ticks,
        OptionalCounter(owner_idle_ns.unwrap_or(u64::MAX)),
        source_groups,
        core::mem::size_of_val(&SHARED.stamps),
        p::NATIVE
    ));
    if let Some((wall_ticks, cpu_ns)) = cpu_control {
        line(format_args!(
            "MPSC {{\"record\":\"cpu-control\",\"wall_ticks\":{},\"cpu_ns\":{}}}",
            wall_ticks, cpu_ns
        ));
    }
    irq_probe::emit();
    for (stage, h) in [("before", before), ("live", live), ("after", after)] {
        if let Some(h) = h {
            line(format_args!(
                "MPSC {{\"record\":\"heap\",\"stage\":\"{stage}\",\"available\":true,\"scope\":\"{}\",\"used\":{},\"peak_since_boot\":{},\"free\":{},\"largest_free\":{}}}",
                p::HEAP_SCOPE,
                h[0],
                h[1],
                h[2],
                h[3]
            ));
        } else {
            line(format_args!(
                "MPSC {{\"record\":\"heap\",\"stage\":\"{stage}\",\"available\":false,\"scope\":\"unavailable\",\"used\":null,\"peak_since_boot\":null,\"free\":null,\"largest_free\":null}}"
            ));
        }
    }
    for role in 0..producers + 2 {
        let r = &SHARED.roles[role];
        let s = stacks[role];
        line(format_args!(
            "MPSC {{\"record\":\"participant\",\"role\":{},\"tid\":{},\"start_priority\":{},\"end_priority\":{},\"start_policy\":{},\"end_policy\":{},\"cpu_ns\":{},\"stack_bytes\":{},\"stack_used\":{},\"internal\":{}}}",
            role,
            r.tid.load(Acquire),
            r.start_priority.load(Acquire),
            r.end_priority.load(Acquire),
            r.start_policy.load(Acquire),
            r.end_policy.load(Acquire),
            OptionalCounter(cpu[role]),
            s[0],
            s[1],
            s[2]
        ));
    }
    arrivals::emit();
    let mut admitted = 0;
    let mut handled = 0;
    let mut checksum = 0u32;
    let retained = if mode == Mode::Sustained {
        WARM
    } else {
        WARM + samples + prefix
    };
    for id in 0..retained {
        let s = &SHARED.stamps[id];
        let flags = s.flags.load(Acquire);
        check(matches!(flags, 5 | 11), "lost-owned-message");
        if flags & 2 != 0 {
            admitted += 1;
        }
        if flags & 8 != 0 {
            handled += 1;
            checksum = checksum.wrapping_add(id as u32);
        }
        line(format_args!(
            "MPSC {{\"record\":\"sample\",\"id\":{},\"planned\":{},\"offered\":{},\"returned\":{},\"received\":{},\"finished\":{},\"order\":{},\"flags\":{}}}",
            id,
            s.planned.load(Acquire),
            s.offered.load(Acquire),
            s.returned.load(Acquire),
            s.received.load(Acquire),
            s.finished.load(Acquire),
            s.order.load(Acquire),
            flags
        ));
    }
    if mode == Mode::Sustained {
        admitted += samples;
        handled += samples;
        checksum = checksum.wrapping_add(SHARED.sustained_checksum.load(Acquire));
        for producer in 0..producers {
            let count = SHARED.sustained_counts[producer].load(Acquire);
            check(
                count as usize == samples / producers,
                "sustained-producer-count",
            );
            line(format_args!(
                "MPSC {{\"record\":\"producer-final\",\"producer\":{},\"count\":{}}}",
                producer, count
            ));
        }
    }
    check(admitted == handled, "undrained-message");
    for _ in 0..64 {
        let a = p::clock();
        let b = p::clock();
        line(format_args!(
            "MPSC {{\"record\":\"counter-pair\",\"ticks\":{}}}",
            b.wrapping_sub(a)
        ));
    }
    line(format_args!(
        "MPSC {{\"record\":\"result\",\"joined\":true,\"admitted\":{},\"handled\":{},\"checksum\":{},\"success\":true}}",
        admitted, handled, checksum
    ));
    line(format_args!("MPSC_DONE"));
}

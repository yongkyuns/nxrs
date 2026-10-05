#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]
#![cfg_attr(target_os = "none", feature(asm_experimental_arch))]
mod controls;
mod hal;
mod owned_slot;
#[path = "core.rs"]
mod protocol;
mod scheduling;
mod telemetry;
#[cfg(all(feature = "scheduling-natural", feature = "scheduling-budget"))]
compile_error!("select only one cooperative scheduling policy");
#[cfg(not(target_os = "none"))]
fn main() {}
#[cfg(target_os = "none")]
mod board {
    use super::{controls, hal, owned_slot::Slot, protocol::*, scheduling, telemetry::*};
    use core::{
        cell::RefCell,
        fmt::Write,
        future::poll_fn,
        sync::atomic::{AtomicBool, AtomicU32, Ordering},
        task::{Poll, Waker},
    };
    use embassy_executor::{Spawner, raw::Executor};
    use embassy_sync::{
        blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
        channel::Channel,
        signal::Signal,
    };
    use esp_hal::{
        Async,
        clock::CpuClock,
        time::Instant,
        timer::{
            OneShotTimer,
            systimer::{SystemTimer, Unit, UnitConfig},
            timg::TimerGroup,
        },
        usb_serial_jtag::UsbSerialJtag,
    };
    use static_cell::StaticCell;
    esp_bootloader_esp_idf::esp_app_desc!();
    #[cfg(feature = "mailbox")]
    const QUEUES: usize = 1;
    #[cfg(not(feature = "mailbox"))]
    const QUEUES: usize = 3;
    #[cfg(feature = "mailbox")]
    const CAPACITY: usize = 24;
    #[cfg(not(feature = "mailbox"))]
    const CAPACITY: usize = 8;
    type Inbox = Channel<CriticalSectionRawMutex, Event, CAPACITY>;
    static INBOXES: [[Inbox; QUEUES]; 20] = [const { [const { Inbox::new() }; QUEUES] }; 20];
    static GATES: [Signal<CriticalSectionRawMutex, ()>; 20] = [const { Signal::new() }; 20];
    static DEADLINES: Mutex<CriticalSectionRawMutex, RefCell<[Option<(u32, Waker)>; 20]>> =
        Mutex::new(RefCell::new([const { None }; 20]));
    struct Resources {
        state: State,
        diagnostics: Diagnostics,
    }
    impl Resources {
        const fn new() -> Self {
            Self {
                state: EMPTY_STATE,
                diagnostics: Diagnostics::new(),
            }
        }
    }
    static RESOURCES: [Slot<Resources>; 20] = [const { Slot::new(Resources::new()) }; 20];
    static EXECUTOR: StaticCell<Executor> = StaticCell::new();
    static READY: AtomicU32 = AtomicU32::new(0);
    static DONE: AtomicU32 = AtomicU32::new(0);
    static EPOCH: AtomicU32 = AtomicU32::new(0);
    static PROFILE: AtomicU32 = AtomicU32::new(0);
    static PEAK: AtomicU32 = AtomicU32::new(0);
    static PENDING: AtomicBool = AtomicBool::new(false);
    static RUNNING: AtomicBool = AtomicBool::new(false);
    static CLOCK_DONE: AtomicBool = AtomicBool::new(false);
    static CLOCK_TICKS: AtomicU32 = AtomicU32::new(0);
    static CLOCK_START: Signal<CriticalSectionRawMutex, ()> = Signal::new();
    static LED: Slot<Option<hal::Led>> = Slot::new(None);
    static EXTRA: Slot<Extra> = Slot::new(Extra::new());
    static SCHED_EXTRA: Slot<SchedulingExtra> = Slot::new(SchedulingExtra::new());
    struct SchedulingExtra {
        io: Histogram,
        io_jobs: u32,
        yields: u32,
        work_digest: u32,
    }
    impl SchedulingExtra {
        const fn new() -> Self {
            Self {
                io: EMPTY_HIST,
                io_jobs: 0,
                yields: 0,
                work_digest: 0,
            }
        }
    }
    fn policy() -> &'static str {
        if cfg!(feature = "scheduling-natural") {
            "natural"
        } else if cfg!(feature = "scheduling-budget") {
            "budget"
        } else {
            "event"
        }
    }
    async fn handoff() {
        SCHED_EXTRA.claim().unwrap().yields += 1;
        scheduling::yield_once().await;
    }
    struct Extra {
        work: Histogram,
        jobs: u32,
        hal_calls: u32,
        hal_errors: u32,
    }
    impl Extra {
        const fn new() -> Self {
            Self {
                work: EMPTY_HIST,
                jobs: 0,
                hal_calls: 0,
                hal_errors: 0,
            }
        }
    }
    #[unsafe(no_mangle)]
    fn __pender(_context: *mut ()) {
        PENDING.store(true, Ordering::Release);
    }
    #[inline]
    fn cycles() -> u32 {
        // Wall-clock timestamps must not depend on whether the CPU is active.
        // Express the 16 MHz system timer in the contract's 240 MHz units.
        (SystemTimer::unit_value(Unit::Unit0) as u32).wrapping_mul(15)
    }
    fn now() -> u32 {
        cycles().wrapping_sub(EPOCH.load(Ordering::Acquire))
    }
    fn set_deadline(id: usize, deadline: u32, waker: &Waker) {
        DEADLINES.lock(|d| d.borrow_mut()[id] = Some((deadline, waker.clone())));
    }
    fn clear_deadline(id: usize) {
        DEADLINES.lock(|d| {
            d.borrow_mut()[id] = None;
        });
    }
    // Event policy is the historical control. Natural waits suspend only on
    // Pending. Budget policy adds a handoff only when its budget is spent AND
    // an inbox remains ready; an empty inbox naturally parks the task.
    async fn wait_any(
        id: usize,
        deadline: u32,
        cursor: &mut usize,
        budget: &mut scheduling::Budget,
    ) -> Option<Event> {
        let ready = INBOXES[id].iter().any(|inbox| !inbox.is_empty());
        if policy() == "event" || (policy() == "budget" && ready && budget.exhausted(now())) {
            handoff().await;
            budget.reset(now());
        }
        let mut suspended = false;
        poll_fn(|cx| {
            for offset in 0..QUEUES {
                let q = (*cursor + offset) % QUEUES;
                if let Poll::Ready(event) = INBOXES[id][q].poll_receive(cx) {
                    *cursor = (q + 1) % QUEUES;
                    clear_deadline(id);
                    if suspended {
                        budget.reset(now());
                    }
                    return Poll::Ready(Some(event));
                }
            }
            // Register before checking time; a timer wake cannot be lost.
            set_deadline(id, deadline, cx.waker());
            if now() >= deadline {
                clear_deadline(id);
                if suspended {
                    budget.reset(now());
                }
                Poll::Ready(None)
            } else {
                suspended = true;
                Poll::Pending
            }
        })
        .await
    }
    async fn wait_io(id: usize) {
        let deadline = now() + controls::IO_WAIT_US * ES_HZ;
        poll_fn(|cx| {
            set_deadline(id, deadline, cx.waker());
            if now() >= deadline {
                clear_deadline(id);
                Poll::Ready(())
            } else {
                Poll::Pending
            }
        })
        .await;
    }
    async fn run_work(
        mut value: u32,
        token: u32,
        iterations: u32,
        budget: &mut scheduling::Budget,
    ) -> u32 {
        if !cfg!(feature = "work-chunked") {
            return work_value(value, token, iterations);
        }
        let mut start = 0;
        while start < iterations {
            let count = (iterations - start).min(scheduling::CHUNK_ITERATIONS);
            value = work_value_range(value, token, start, count);
            start += count;
            if start < iterations {
                handoff().await;
                budget.reset(now());
            }
        }
        value
    }
    #[embassy_executor::task]
    async fn clock(mut timer: OneShotTimer<'static, Async>) {
        loop {
            CLOCK_START.wait().await;
            while RUNNING.load(Ordering::Acquire) {
                timer.delay_millis_async(controls::timer_ms()).await;
                CLOCK_TICKS.fetch_add(1, Ordering::Relaxed);
                let time = now();
                DEADLINES.lock(|deadlines| {
                    let mut deadlines = deadlines.borrow_mut();
                    for deadline in deadlines.iter_mut() {
                        if deadline.as_ref().is_some_and(|(at, _)| *at <= time) {
                            let (_, waker) = deadline.take().unwrap();
                            waker.wake();
                        }
                    }
                });
            }
            CLOCK_DONE.store(true, Ordering::Release);
        }
    }
    #[embassy_executor::task(pool_size = 20)]
    async fn service(id: usize) {
        let mut resources = RESOURCES[id].claim().unwrap();
        let mut schedule = Schedule::new(id as u32, PROFILE.load(Ordering::Acquire));
        let mut cursor = 0;
        let mut budget = scheduling::Budget::new(now());
        READY.fetch_add(1, Ordering::Release);
        GATES[id].wait().await;
        budget.reset(now());
        if PROFILE.load(Ordering::Acquire) == 6 {
            drop(resources);
            DONE.fetch_add(1, Ordering::Release);
            return;
        }
        let stop = (ES_DURATION_US + ES_DRAIN_US) * ES_HZ;
        while now() < stop {
            if let Some(release) = schedule.due(now()) {
                for n in 0..release.count {
                    let release = Release {
                        sequence: release.sequence + n,
                        ..release
                    };
                    for peer in 0..3 {
                        let mut event = Event::new(id as u32, peer, &release, 0);
                        let posted = now();
                        event.posted_cycles = posted;
                        let q = if QUEUES == 1 { 0 } else { event.kind as usize };
                        let inbox = &INBOXES[event.destination as usize][q];
                        let result = if inbox.try_send(event).is_ok() { 0 } else { 1 };
                        // Only executor tasks write PEAK; interrupts never do.
                        // This synchronous block cannot interleave another task.
                        // Avoid fetch_max: the pinned Xtensa backend emits
                        // branches to missing labels for its CAS/min lowering.
                        if result == 0 {
                            let seen = inbox.len() as u32;
                            if seen > PEAK.load(Ordering::Relaxed) {
                                PEAK.store(seen, Ordering::Relaxed);
                            }
                        }
                        resources.diagnostics.send(&event, result, posted);
                    }
                }
            }
            let deadline = match schedule.next_deadline() {
                n if n >= ES_DURATION_US * ES_HZ => stop,
                n => n,
            };
            if let Some(event) = wait_any(id, deadline, &mut cursor, &mut budget).await {
                let started = now();
                let mut result = resources.state.handle(&event, id as u32).is_ok();
                let profile = PROFILE.load(Ordering::Acquire);
                if result && id == 0 {
                    if event.kind == 1 {
                        resources.state.value = run_work(
                            resources.state.value,
                            event.token,
                            controls::work_iterations(profile),
                            &mut budget,
                        )
                        .await;
                        if profile == 8 {
                            wait_io(id).await;
                            // The timer wait always suspends. Time spent waiting
                            // is not uninterrupted work in this executor poll.
                            budget.reset(now());
                            let mut extra = SCHED_EXTRA.claim().unwrap();
                            extra.io_jobs += 1;
                            extra.io.add(micros(now() - started));
                        }
                    }
                    if profile == 5 && event.kind == 0 {
                        let mut extra = EXTRA.claim().unwrap();
                        extra.hal_calls += 1;
                        result = LED
                            .claim()
                            .unwrap()
                            .as_mut()
                            .unwrap()
                            .apply(event.token & 1 != 0)
                            .is_ok();
                        if !result {
                            extra.hal_errors += 1;
                        }
                    }
                }
                let finished = now();
                if result && id == 0 && event.kind == 1 && controls::work_iterations(profile) != 0 {
                    let mut extra = EXTRA.claim().unwrap();
                    extra.jobs += 1;
                    extra.work.add(micros(finished - started));
                    let mut scheduling_extra = SCHED_EXTRA.claim().unwrap();
                    scheduling_extra.work_digest = scheduling_extra
                        .work_digest
                        .wrapping_add(resources.state.value);
                }
                resources
                    .diagnostics
                    .receive(&event, result, started, finished);
                budget.handled();
            }
        }
        resources.diagnostics.pending =
            u32::from(schedule.next_deadline() < ES_DURATION_US * ES_HZ);
        clear_deadline(id);
        drop(resources);
        DONE.fetch_add(1, Ordering::Release);
    }
    fn spawn(spawner: Spawner) {
        for id in 0..20 {
            spawner.spawn(service(id).unwrap());
        }
    }
    fn poll(executor: &'static Executor) {
        unsafe {
            executor.poll();
        }
    }
    fn sleep_if_idle() {
        // No critical-section guard spans WAITI: it lowers INTLEVEL while
        // sleeping. Mask the flag check to avoid losing an interrupt between
        // checking for work and entering idle. The caller must still need work
        // and have a hardware alarm armed; completion must be checked first.
        let previous: u32;
        unsafe {
            core::arch::asm!("rsil {0}, 5",out(reg)previous);
        }
        core::sync::atomic::compiler_fence(Ordering::SeqCst);
        if !PENDING.load(Ordering::Acquire) {
            unsafe {
                core::arch::asm!("waiti 0", options(nostack));
            }
        }
        core::sync::atomic::compiler_fence(Ordering::SeqCst);
        unsafe {
            core::arch::asm!("wsr.ps {0}","rsync",in(reg)previous);
        }
    }
    fn run(console: &mut impl Write, executor: &'static Executor, profile: u32) -> bool {
        *EXTRA.claim().unwrap() = Extra::new();
        *SCHED_EXTRA.claim().unwrap() = SchedulingExtra::new();
        for id in 0..20 {
            assert!(INBOXES[id].iter().all(Inbox::is_empty));
            GATES[id].reset();
            *RESOURCES[id].claim().unwrap() = Resources::new();
        }
        DEADLINES.lock(|d| {
            *d.borrow_mut() = [const { None }; 20];
        });
        READY.store(0, Ordering::Release);
        DONE.store(0, Ordering::Release);
        PROFILE.store(profile, Ordering::Release);
        PEAK.store(0, Ordering::Release);
        RUNNING.store(true, Ordering::Release);
        CLOCK_DONE.store(false, Ordering::Release);
        CLOCK_TICKS.store(0, Ordering::Relaxed);
        CLOCK_START.signal(());
        spawn(executor.spawner());
        let wall = Instant::now();
        while READY.load(Ordering::Acquire) != 20 {
            PENDING.store(false, Ordering::Release);
            poll(executor);
        }
        // Untimed startup qualification: prove the hardware timer can wake its
        // future before release. Fail visibly instead of trusting idle wakeup.
        writeln!(console, "ES_STAGE services_ready=20").unwrap();
        while CLOCK_TICKS.load(Ordering::Relaxed) == 0 {
            PENDING.store(false, Ordering::Release);
            poll(executor);
            assert!(wall.elapsed().as_millis() < 1000);
        }
        writeln!(console, "ES_STAGE timer_ready=1").unwrap();
        EPOCH.store(cycles(), Ordering::Release);
        for gate in &GATES {
            gate.signal(());
        }
        while DONE.load(Ordering::Acquire) != 20 {
            PENDING.store(false, Ordering::Release);
            poll(executor);
            if DONE.load(Ordering::Acquire) != 20 {
                sleep_if_idle();
            }
            assert!(wall.elapsed().as_millis() < 16000);
        }
        RUNNING.store(false, Ordering::Release);
        while !CLOCK_DONE.load(Ordering::Acquire) {
            PENDING.store(false, Ordering::Release);
            poll(executor);
            if !CLOCK_DONE.load(Ordering::Acquire) {
                sleep_if_idle();
            }
        }
        report(console, profile)
    }
    fn report(console: &mut impl Write, profile: u32) -> bool {
        let (
            mut attempted,
            mut accepted,
            mut received,
            mut rejected,
            mut errors,
            mut missed,
            mut finish,
            mut worst,
        ) = (0u32, 0u32, 0u32, 0u32, 0u32, 0u32, 0u32, 0u32);
        let (mut publication, mut start, mut done, mut control) =
            (EMPTY_HIST, EMPTY_HIST, EMPTY_HIST, EMPTY_HIST);
        let mut queue_start = EMPTY_HIST;
        for id in 0..20 {
            let resource = RESOURCES[id].claim().unwrap();
            let d = &resource.diagnostics;
            attempted += d.attempted;
            rejected += d.rejected;
            errors += d.errors + d.pending;
            missed += d.missed.iter().sum::<u32>();
            finish = finish.max(d.last_finish);
            merge(&mut publication, &d.publication);
            merge(&mut start, &d.start);
            merge(&mut done, &d.finish);
            merge(&mut control, &d.control_start);
            merge(&mut queue_start, &d.queue_start);
            let p99 = d.start.percentile(99);
            worst = worst.max(p99);
            for peer in 0..3 {
                let destination = destination(id as u32, peer) as usize;
                let receiver = RESOURCES[destination].claim().unwrap();
                for kind in 0..3 {
                    let index = peer as usize * 3 + kind;
                    let sent = d.accepted[index];
                    let got = receiver.state.received[index];
                    accepted += sent.count;
                    received += resource.state.received[index].count;
                    if sent != got {
                        errors += 1;
                    }
                }
            }
            writeln!(console,"ES_SERVICE id={} received={} start_p99_us={} start_max_us={} finish_p99_us={} control_p99_us={} missed_control={} missed_data={} missed_status={} rejected={} queue_p99_us={} queue_max_us={}",
                id,d.start.count,p99,d.start.maximum,d.finish.percentile(99),d.control_start.percentile(99),d.missed[0],d.missed[1],d.missed[2],d.rejected,d.queue_start.percentile(99),d.queue_start.maximum).unwrap();
        }
        if attempted != accepted + rejected || accepted != received {
            errors += 1;
        }
        if INBOXES.iter().flatten().any(|q| !q.is_empty()) {
            errors += 1;
        }
        let queue_storage = core::mem::size_of_val(&INBOXES);
        writeln!(console,"ES_RESOURCES queue_buffers=30720 queue_objects={} thread_objects=0 entry_storage=0 stack_storage=8192 heap_allocated=0 diagnostics_queue_peaks=4 note=queue_peak_observation_may_underestimate",
            queue_storage-30720).unwrap();
        writeln!(
            console,
            "ES_MEMORY application_state={} diagnostics={} heap_before=0 heap_live=0",
            20 * core::mem::size_of::<State>(),
            20 * core::mem::size_of::<Diagnostics>()
        )
        .unwrap();
        let extra = EXTRA.claim().unwrap();
        writeln!(console, "ES_CONTROL timer_ms={} work_iterations={} work_jobs={} work_p99_us={} work_max_us={} hal_calls={} hal_errors={} diagnostic_bytes={}", controls::timer_ms(), controls::work_iterations(profile), extra.jobs, extra.work.percentile(99), extra.work.maximum, extra.hal_calls, extra.hal_errors, core::mem::size_of::<Extra>()).unwrap();
        let scheduling_extra = SCHED_EXTRA.claim().unwrap();
        writeln!(console, "ES_SCHED policy={} work_mode={} budget_events={} budget_us={} chunk_iterations={} io_wait_us={} yields={} io_jobs={} io_p99_us={} io_max_us={} work_digest={} diagnostic_bytes={}",
            policy(), if cfg!(feature = "work-chunked") { "chunked" } else { "monolithic" },
            scheduling::BUDGET_EVENTS, scheduling::BUDGET_US, scheduling::CHUNK_ITERATIONS,
            controls::IO_WAIT_US, scheduling_extra.yields, scheduling_extra.io_jobs,
            scheduling_extra.io.percentile(99), scheduling_extra.io.maximum, scheduling_extra.work_digest,
            core::mem::size_of::<SchedulingExtra>()).unwrap();
        if profile == 5 {
            LED.claim().unwrap().as_mut().unwrap().turn_off().unwrap();
        }
        writeln!(console,"ES_RESULT platform=embassy profile={} services=20 queues={} event_bytes=64 slots=480 attempted={} accepted={} received={} rejected={} errors={} missed={} publication_p99_us={} publication_max_us={} start_p99_us={} start_max_us={} finish_p99_us={} finish_max_us={} control_p99_us={} control_max_us={} worst_service_p99_us={} queue_peak_observed={} last_finish_us={} delivery_ok={} capacity_ok={} deadlines_ok={} queue_p99_us={} queue_max_us={}",
            controls::PROFILES[profile as usize],20*QUEUES,attempted,accepted,received,rejected,errors,missed,
            publication.percentile(99),publication.maximum,start.percentile(99),start.maximum,done.percentile(99),done.maximum,
            control.percentile(99),control.maximum,worst,PEAK.load(Ordering::Relaxed),micros(finish),u32::from(errors==0),u32::from(rejected==0),u32::from(missed==0),queue_start.percentile(99),queue_start.maximum).unwrap();
        writeln!(
            console,
            "{}",
            if errors == 0 {
                "ES_PASS"
            } else {
                "ES_FAIL stage=protocol"
            }
        )
        .unwrap();
        errors == 0
    }
    fn saturation_event(destination: usize, kind: usize, sequence: u32) -> Event {
        Event::new(
            (destination as u32 + 19) % 20,
            0,
            &Release {
                scheduled_cycles: 0,
                sequence,
                count: 1,
                kind: kind as u32,
            },
            0,
        )
    }
    fn saturation(console: &mut impl Write, executor: &'static Executor) -> bool {
        // The same 20 service task instances remain parked on their start
        // gates while every channel is full. Their static futures are included
        // in ELF RAM accounting, not replaced by a coordinator-only shortcut.
        for id in 0..20 {
            assert!(INBOXES[id].iter().all(Inbox::is_empty));
            GATES[id].reset();
            *RESOURCES[id].claim().unwrap() = Resources::new();
        }
        PROFILE.store(6, Ordering::Release);
        READY.store(0, Ordering::Release);
        DONE.store(0, Ordering::Release);
        spawn(executor.spawner());
        while READY.load(Ordering::Acquire) != 20 {
            poll(executor);
        }
        let mut errors = 0u32;
        for cycle in 0..3 {
            let (mut filled, mut drained, mut overflow_rejected) = (0, 0, 0);
            let mut sequence = [0u32; 3];
            for destination in 0..20 {
                for q in 0..QUEUES {
                    for slot in 0..CAPACITY {
                        let kind = if QUEUES == 1 { slot % 3 } else { q };
                        let event = saturation_event(destination, kind, sequence[kind]);
                        sequence[kind] += 1;
                        if INBOXES[destination][q].try_send(event).is_ok() {
                            filled += 1;
                        } else {
                            errors += 1;
                        }
                    }
                }
            }
            let depth_full = INBOXES
                .iter()
                .flatten()
                .filter(|q| q.len() == CAPACITY)
                .count();
            if filled != 480 || depth_full != 20 * QUEUES {
                errors += 1;
            }
            for destination in 0..20 {
                for q in 0..QUEUES {
                    let before = INBOXES[destination][q].len();
                    let overflow = saturation_event(destination, q, u32::MAX);
                    if INBOXES[destination][q].try_send(overflow).is_err() {
                        overflow_rejected += 1;
                    } else {
                        errors += 1;
                    }
                    if before != CAPACITY || INBOXES[destination][q].len() != before {
                        errors += 1;
                    }
                }
            }
            writeln!(console,"ES_RESOURCES queue_buffers=30720 queue_objects={} thread_objects=0 entry_storage=0 stack_storage=8192 heap_allocated=0 diagnostics_queue_peaks=4 note=static_arena_counted_in_ELF", core::mem::size_of_val(&INBOXES) - 30720).unwrap();
            for destination in 0..20 {
                let mut cursor = 0;
                let mut per_kind = [0u32; 3];
                for slot in 0..24 {
                    let mut got = None;
                    for offset in 0..QUEUES {
                        let q = (cursor + offset) % QUEUES;
                        if let Ok(event) = INBOXES[destination][q].try_receive() {
                            cursor = (q + 1) % QUEUES;
                            got = Some(event);
                            break;
                        }
                    }
                    if let Some(event) = got {
                        drained += 1;
                        let kind = if QUEUES == 1 {
                            slot % 3
                        } else {
                            event.kind as usize
                        };
                        if kind >= 3 {
                            errors += 1;
                            continue;
                        }
                        let seq = destination as u32 * 8 + per_kind[kind];
                        if event != saturation_event(destination, kind, seq) {
                            errors += 1;
                        }
                        per_kind[kind] += 1;
                    } else {
                        errors += 1;
                    }
                }
            }
            let depth_zero = INBOXES.iter().flatten().filter(|q| q.is_empty()).count();
            if drained != 480 || depth_zero != 20 * QUEUES {
                errors += 1;
            }
            writeln!(console,"ES_SAT_CYCLE cycle={} empty_heap=0 full_heap=0 drained_heap=0 filled={} drained={} overflow_rejected={} depth_full={} depth_zero={} errors={}", cycle,filled,drained,overflow_rejected,depth_full,depth_zero,errors).unwrap();
        }
        for gate in &GATES {
            gate.signal(());
        }
        while DONE.load(Ordering::Acquire) != 20 {
            poll(executor);
        }
        writeln!(console,"ES_SAT_RESULT platform=embassy queues={} slots=480 event_bytes=64 cycles=3 errors={} stacks=8192",20*QUEUES,errors).unwrap();
        writeln!(
            console,
            "{}",
            if errors == 0 {
                "ES_PASS"
            } else {
                "ES_FAIL stage=saturation"
            }
        )
        .unwrap();
        errors == 0
    }
    #[esp_hal::main]
    fn main() -> ! {
        let peripherals = esp_hal::init(esp_hal::Config::default().with_cpu_clock(CpuClock::max()));
        // SAFETY: no other task or alarm uses Unit0's stall configuration;
        // keep the counter enabled through idle, without resetting its value.
        unsafe {
            SystemTimer::configure_unit(Unit::Unit0, UnitConfig::Enabled);
        }
        let mut console = UsbSerialJtag::new(peripherals.USB_DEVICE);
        *LED.claim().unwrap() = Some(hal::Led::new(peripherals.GPIO2));
        let executor = EXECUTOR.init(Executor::new(core::ptr::null_mut()));
        // One persistent task owns the unique timer token for its full life.
        // It parks between runs. No type conversion, lifetime extension, or
        // duplicate token is needed (the pinned HAL's into_blocking is mistyped).
        let timer = OneShotTimer::new(TimerGroup::new(peripherals.TIMG0).timer0).into_async();
        executor.spawner().spawn(clock(timer).unwrap());
        writeln!(console, "EVENT_SERVICES_READY platform=embassy").unwrap();
        write!(console, "event> ").unwrap();
        let mut line = [0u8; 16];
        let mut used = 0;
        loop {
            if let Ok(byte) = console.read_byte() {
                if byte == b'\r' || byte == b'\n' {
                    if used == 0 {
                        write!(console, "event> ").unwrap();
                        continue;
                    }
                    let profile = match &line[..used] {
                        b"normal" => Some(0),
                        b"burst" => Some(1),
                        b"overload" => Some(2),
                        b"work-short" => Some(3),
                        b"work-long" => Some(4),
                        b"hal" => Some(5),
                        b"work-medium" => Some(7),
                        b"io-wait" => Some(8),
                        _ => None,
                    };
                    let success = if &line[..used] == b"saturation" {
                        saturation(&mut console, executor)
                    } else if let Some(profile) = profile {
                        run(&mut console, executor, profile)
                    } else {
                        false
                    };
                    writeln!(console, "ES_COMMAND_EXIT status={}", u32::from(!success)).unwrap();
                    if !success {
                        loop {
                            core::hint::spin_loop();
                        }
                    }
                    used = 0;
                    write!(console, "event> ").unwrap();
                } else if used < line.len() {
                    line[used] = byte;
                    used += 1;
                }
            }
        }
    }
    #[panic_handler]
    fn panic(info: &core::panic::PanicInfo) -> ! {
        let mut console = UsbSerialJtag::new(unsafe { esp_hal::peripherals::USB_DEVICE::steal() });
        writeln!(console, "ES_FAIL panic={}", info).ok();
        loop {
            core::hint::spin_loop();
        }
    }
}

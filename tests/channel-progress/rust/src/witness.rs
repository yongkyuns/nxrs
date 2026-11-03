//! Force interleavings in the exact privately instrumented Crossbeam source.
//! Gates only release participants; they do not stand in for channel storage,
//! selection registration, publication, or reclamation.
use crate::{
    native::{self, Gate, Result, Schedule, State},
    Message, Mode, CAPACITY, OBSERVER_PRIORITY, WAIT_MS, WAKER_PRIORITY, WORKER_STACK,
};
use crossbeam_channel::{bounded, Receiver, Sender, TrySendError};
use std::{
    ffi::c_void,
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicPtr, AtomicU32, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};

#[derive(Default)]
struct Stamp {
    low: AtomicU32,
    high: AtomicU32,
}
impl Stamp {
    fn store(&self, value: u64) {
        self.low.store(value as u32, Ordering::Relaxed);
        self.high.store((value >> 32) as u32, Ordering::Relaxed);
    }
    fn load(&self) -> u64 {
        (u64::from(self.high.load(Ordering::Relaxed)) << 32)
            | u64::from(self.low.load(Ordering::Relaxed))
    }
}

static HOOK_STAGE: AtomicU32 = AtomicU32::new(0);
static HOOK_TID: AtomicI32 = AtomicI32::new(-1);
static HOOK_GATE: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());
static HOOK_HITS: AtomicU32 = AtomicU32::new(0);
static HOOK_RETURNED: AtomicBool = AtomicBool::new(false);
static HOOK_ERROR: AtomicBool = AtomicBool::new(false);
static HOOK_STAMP: Stamp = Stamp {
    low: AtomicU32::new(0),
    high: AtomicU32::new(0),
};
static SELECT_OWNER: AtomicI32 = AtomicI32::new(-1);
static SELECT_REGISTERED_ARMS: AtomicU32 = AtomicU32::new(0);

// This ABI exists only to connect a private source-pinned dependency to its
// witness. It is absent from ordinary Crossbeam sources and production images.
#[no_mangle]
pub extern "C" fn nxrs_crossbeam_progress_hook(stage: u32) {
    if stage >= 0x100 {
        if native::tid() == SELECT_OWNER.load(Ordering::Acquire) {
            SELECT_REGISTERED_ARMS.store(stage - 0x100, Ordering::Release);
        }
        return;
    }
    if stage == 0
        || native::tid() != HOOK_TID.load(Ordering::Acquire)
        || HOOK_STAGE
            .compare_exchange(stage, 0, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
    {
        return;
    }
    match native::monotonic_ns() {
        Ok(stamp) => HOOK_STAMP.store(stamp),
        Err(_) => HOOK_ERROR.store(true, Ordering::Release),
    }
    HOOK_HITS.store(1, Ordering::Release);
    let gate = HOOK_GATE.load(Ordering::Acquire);
    // A native notification wakes the higher-priority peer (or the independent
    // selection waker). The callback never waits and never changes priority.
    if gate.is_null() || unsafe { native::scp_gate_post(gate) } != 0 {
        HOOK_ERROR.store(true, Ordering::Release);
    }
    HOOK_RETURNED.store(true, Ordering::Release);
}

fn arm(stage: u32, tid: i32, gate: &Gate) {
    HOOK_HITS.store(0, Ordering::Relaxed);
    HOOK_RETURNED.store(false, Ordering::Relaxed);
    HOOK_ERROR.store(false, Ordering::Relaxed);
    HOOK_TID.store(tid, Ordering::Relaxed);
    HOOK_GATE.store(gate.pointer(), Ordering::Release);
    HOOK_STAGE.store(stage, Ordering::Release);
}

struct Worker {
    tid: AtomicI32,
    entered: AtomicBool,
    entered_stamp: Stamp,
    done: AtomicBool,
}
impl Worker {
    fn new() -> Self {
        Self {
            tid: AtomicI32::new(-1),
            entered: AtomicBool::new(false),
            entered_stamp: Stamp::default(),
            done: AtomicBool::new(false),
        }
    }
    fn setup(&self, priority: i32, ready: &Gate) -> Result<()> {
        let tid = native::tid();
        let result = native::fifo(tid, priority);
        if result.is_ok() {
            self.tid.store(tid, Ordering::Release);
        }
        ready.post()?;
        result.map(|_| ())
    }
    fn enter(&self) -> Result<()> {
        self.entered_stamp.store(native::monotonic_ns()?);
        self.entered.store(true, Ordering::Release);
        Ok(())
    }
    fn schedule(&self) -> Result<Schedule> {
        let tid = self.tid.load(Ordering::Acquire);
        if tid < 1 {
            return Err("worker-scheduling");
        }
        native::schedule(tid)
    }
    fn snapshot(&self) -> Snapshot {
        let tid = self.tid.load(Ordering::Acquire);
        if tid < 1 {
            Snapshot::unavailable()
        } else {
            let (state, priority) = native::state(tid);
            Snapshot { state, priority }
        }
    }
    fn done(&self) -> bool {
        self.done.load(Ordering::Acquire)
    }
}

struct Shared {
    sender: Worker,
    receiver: Worker,
    waker: Worker,
    cancelled: AtomicBool,
    valid: AtomicBool,
    wake_consumed: AtomicBool,
    sent: AtomicU32,
    received: AtomicU32,
}
impl Shared {
    fn new(prefilled: u32) -> Self {
        Self {
            sender: Worker::new(),
            receiver: Worker::new(),
            waker: Worker::new(),
            cancelled: AtomicBool::new(false),
            valid: AtomicBool::new(true),
            wake_consumed: AtomicBool::new(false),
            sent: AtomicU32::new(prefilled),
            received: AtomicU32::new(0),
        }
    }
    fn receive(&self, message: Message, expected: u32) -> Result<()> {
        if !message.valid(expected) {
            self.valid.store(false, Ordering::Release);
            return Err("payload-mismatch");
        }
        self.received.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

#[derive(Clone, Copy)]
struct Snapshot {
    state: State,
    priority: i32,
}
impl Snapshot {
    fn waiting_at(self, priority: i32) -> bool {
        self.state == State::WaitSem && self.priority == priority
    }
    fn unavailable() -> Self {
        Self {
            state: State::Unavailable,
            priority: -1,
        }
    }
}

enum Selected {
    Stop,
    Important(Message),
    Ordinary(Message),
}
fn select(
    stop: &Receiver<()>,
    important: &Receiver<Message>,
    ordinary: &Receiver<Message>,
) -> Result<Selected> {
    // The same biased order as the selected Wi-Fi owner contract. The deadline
    // bounds ordinary blocking; it is deliberately not assumed to stop a spin
    // inside a channel's reservation path.
    crossbeam_channel::select_biased! {
        recv(stop) -> value => { value.map_err(|_| "stop-disconnected")?; Ok(Selected::Stop) },
        recv(important) -> value => value.map(Selected::Important).map_err(|_| "important-disconnected"),
        recv(ordinary) -> value => value.map(Selected::Ordinary).map_err(|_| "ordinary-disconnected"),
        default(Duration::from_millis(WAIT_MS.into())) => Err("selected-wait-deadline"),
    }
}

pub struct Report {
    pub sender: Schedule,
    pub receiver: Schedule,
    pub waker: Schedule,
    pub observer: Schedule,
    hook_hits: u32,
    hook_returned: bool,
    hook_error: bool,
    consumer_entered: bool,
    producer_entered: bool,
    consumer_done: bool,
    producer_done: bool,
    waker_done: bool,
    consumer_state: Snapshot,
    producer_state: Snapshot,
    waker_state: Snapshot,
    hook_ns: u64,
    entered_ns: u64,
    observer_ns: u64,
    observation_ns: u64,
    gates_verified: bool,
    select_verified: bool,
    registered_arms: u32,
    wake_consumed: bool,
    recovery: bool,
    delivered: bool,
    sent: u32,
    received: u32,
    stall: bool,
    completed: bool,
    success: bool,
}

fn release(gates: &[&Gate]) {
    for gate in gates {
        if gate.post().is_err() {
            crate::fatal("error-release-failed");
        }
    }
}

fn cancel(shared: &Shared, mode: Mode, stop: &Sender<()>, gates: &[&Gate]) {
    shared.cancelled.store(true, Ordering::Release);
    // Native gates cannot wake an owner already blocked in Crossbeam. The
    // stop queue has one observer producer and is unused by the measured
    // interleaving, so this bounded try-send cannot wait for a reserved slot.
    // A disconnected receiver has already left the selected wait.
    if mode.selected() {
        match stop.try_send(()) {
            Ok(()) | Err(TrySendError::Disconnected(())) => {}
            Err(TrySendError::Full(())) => crate::fatal("error-stop-wake-unexpectedly-full"),
        }
    }
    release(gates);
}

fn recover(mode: Mode, shared: &Shared) -> Result<bool> {
    let low = if mode.reverse() {
        &shared.receiver
    } else {
        &shared.sender
    };
    let high = if mode.reverse() {
        &shared.sender
    } else {
        &shared.receiver
    };
    let mut changed = false;
    // Only the observer invokes this after the unrepaired snapshot. Both
    // recovery priorities remain below the independently runnable observer.
    for (worker, priority) in [(low, 200), (high, 100)] {
        if !worker.done() {
            let tid = worker.tid.load(Ordering::Acquire);
            if tid > 0 && native::state(tid).0 != State::Absent {
                native::fifo(tid, priority)?;
                changed = true;
            }
        }
    }
    Ok(changed)
}

pub fn run(mode: Mode) -> Result<Report> {
    if !native::is_nuttx() {
        return Err("no-independent-tcb-witness");
    }
    let observer = native::fifo(native::tid(), OBSERVER_PRIORITY)?;
    // NuttX built-ins can share static storage across sequential invocations.
    // The capture contract permits only one active witness command per boot.
    HOOK_STAGE.store(0, Ordering::Release);
    HOOK_GATE.store(std::ptr::null_mut(), Ordering::Release);
    HOOK_TID.store(-1, Ordering::Release);
    SELECT_OWNER.store(-1, Ordering::Release);
    SELECT_REGISTERED_ARMS.store(0, Ordering::Release);
    let ready = Gate::new()?;
    let producer_start = Gate::new()?;
    let consumer_start = Gate::new()?;
    let waker_start = Gate::new()?;
    let shared = Arc::new(Shared::new(if mode.reverse() {
        CAPACITY as u32
    } else {
        0
    }));
    let (sender, receiver) = bounded::<Message>(CAPACITY);
    if mode.reverse() {
        for sequence in 1..=CAPACITY as u32 {
            sender
                .send(Message::new(sequence))
                .map_err(|_| "prefill-disconnected")?;
        }
    }
    let (stop_sender, stop_receiver) = bounded::<()>(1);
    let (wake_sender, wake_receiver) = bounded::<Message>(CAPACITY);

    let (worker, ready_worker, start_worker) =
        (shared.clone(), ready.clone(), consumer_start.clone());
    let consumer = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            worker
                .receiver
                .setup(mode.receiver_priority(), &ready_worker)?;
            if mode.selected() {
                SELECT_OWNER.store(native::tid(), Ordering::Release);
            }
            start_worker.wait(WAIT_MS)?;
            if worker.cancelled.load(Ordering::Acquire) {
                return Ok(());
            }
            if mode.selected() {
                // No fixture wait lies between this flag and Crossbeam's own wait.
                // The observer also requires its all-three-registered source marker.
                worker.receiver.enter()?;
                match select(&stop_receiver, &receiver, &wake_receiver)? {
                    Selected::Ordinary(message) if message.valid(0) => {
                        worker.wake_consumed.store(true, Ordering::Release)
                    }
                    Selected::Stop if worker.cancelled.load(Ordering::Acquire) => return Ok(()),
                    _ => return Err("independent-selected-wake-not-consumed"),
                }
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                // Re-enter the actual owner selection after processing the ordinary
                // notification. The important producer is still inside its hook.
                worker.receiver.enter()?;
                match select(&stop_receiver, &receiver, &wake_receiver)? {
                    Selected::Important(message) => worker.receive(message, 1)?,
                    _ => return Err("selected-target-arm-mismatch"),
                }
            } else {
                worker.receiver.enter()?;
                for sequence in 1..=mode.messages() as u32 {
                    let message = receiver.recv().map_err(|_| "direct-receive-disconnected")?;
                    worker.receive(message, sequence)?;
                }
            }
            worker.receiver.done.store(true, Ordering::Release);
            Ok(())
        })
        .map_err(|_| "consumer-spawn")?;

    let (worker, ready_worker, start_worker) =
        (shared.clone(), ready.clone(), producer_start.clone());
    let producer = match thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            worker.sender.setup(mode.sender_priority(), &ready_worker)?;
            start_worker.wait(WAIT_MS)?;
            if worker.cancelled.load(Ordering::Acquire) {
                return Ok(());
            }
            worker.sender.enter()?;
            sender
                .send(Message::new(if mode.reverse() {
                    CAPACITY as u32 + 1
                } else {
                    1
                }))
                .map_err(|_| "direct-send-disconnected")?;
            worker.sent.fetch_add(1, Ordering::Relaxed);
            worker.sender.done.store(true, Ordering::Release);
            Ok(())
        }) {
        Ok(handle) => handle,
        Err(_) => {
            shared.cancelled.store(true, Ordering::Release);
            release(&[&consumer_start]);
            crate::join_bounded(consumer)?;
            return Err("producer-spawn");
        }
    };

    let waker = if mode.selected() {
        let (worker, ready_worker, start_worker) =
            (shared.clone(), ready.clone(), waker_start.clone());
        match thread::Builder::new()
            .stack_size(WORKER_STACK)
            .spawn(move || {
                worker.waker.setup(WAKER_PRIORITY, &ready_worker)?;
                start_worker.wait(WAIT_MS)?;
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                worker.waker.enter()?;
                wake_sender
                    .send(Message::new(0))
                    .map_err(|_| "independent-wake-disconnected")?;
                worker.waker.done.store(true, Ordering::Release);
                Ok(())
            }) {
            Ok(handle) => Some(handle),
            Err(_) => {
                shared.cancelled.store(true, Ordering::Release);
                release(&[&producer_start, &consumer_start]);
                let joined_producer = crate::join_bounded(producer);
                let joined_consumer = crate::join_bounded(consumer);
                joined_producer?;
                joined_consumer?;
                return Err("waker-spawn");
            }
        }
    } else {
        None
    };

    let observed = (|| {
        for _ in 0..if mode.selected() { 3 } else { 2 } {
            ready.wait(WAIT_MS)?;
        }
        let sender = shared.sender.schedule()?;
        let receiver = shared.receiver.schedule()?;
        let waker = if mode.selected() {
            shared.waker.schedule()?
        } else {
            Schedule {
                policy: -1,
                priority: -1,
            }
        };
        if sender.policy != 1
            || sender.priority != mode.sender_priority()
            || receiver.policy != 1
            || receiver.priority != mode.receiver_priority()
            || (mode.selected() && (waker.policy != 1 || waker.priority != WAKER_PRIORITY))
        {
            return Err("worker-priority-contract");
        }
        crate::wait_until(|| {
            shared.sender.snapshot().waiting_at(mode.sender_priority())
                && shared
                    .receiver
                    .snapshot()
                    .waiting_at(mode.receiver_priority())
                && (!mode.selected() || shared.waker.snapshot().waiting_at(WAKER_PRIORITY))
        })?;

        if mode.selected() {
            consumer_start.post()?;
            crate::wait_until(|| {
                shared.receiver.entered.load(Ordering::Acquire)
                    && SELECT_REGISTERED_ARMS.load(Ordering::Acquire) == 3
                    && shared
                        .receiver
                        .snapshot()
                        .waiting_at(mode.receiver_priority())
            })?;
        }
        let low_tid = if mode.reverse() {
            shared.receiver.tid.load(Ordering::Acquire)
        } else {
            shared.sender.tid.load(Ordering::Acquire)
        };
        let released = if mode.selected() {
            &waker_start
        } else if mode.reverse() {
            &producer_start
        } else {
            &consumer_start
        };
        arm(mode.stage(), low_tid, released);
        if mode.reverse() {
            consumer_start.post()?;
        } else {
            producer_start.post()?;
        }
        // Only the source hook releases the high peer/independent waker. This
        // sleep schedules the observer's snapshot, never the interleaving.
        thread::sleep(Duration::from_millis(100));
        let observer_ns = native::monotonic_ns()?;
        let hook_hits = HOOK_HITS.load(Ordering::Acquire);
        let consumer_entered = shared.receiver.entered.load(Ordering::Acquire);
        let producer_entered = shared.sender.entered.load(Ordering::Acquire);
        let high = if mode.reverse() {
            &shared.sender
        } else {
            &shared.receiver
        };
        let entered_ns = high.entered_stamp.load();
        let hook_ns = HOOK_STAMP.load();
        let observation_ns = observer_ns.saturating_sub(entered_ns);
        let consumer_done = shared.receiver.done();
        let producer_done = shared.sender.done();
        let waker_done = !mode.selected() || shared.waker.done();
        let hook_returned = HOOK_RETURNED.load(Ordering::Acquire);
        let hook_error = HOOK_ERROR.load(Ordering::Acquire);
        let consumer_state = shared.receiver.snapshot();
        let producer_state = shared.sender.snapshot();
        let waker_state = shared.waker.snapshot();
        let wake_consumed = shared.wake_consumed.load(Ordering::Acquire);
        let registered_arms = SELECT_REGISTERED_ARMS.load(Ordering::Acquire);
        let common = hook_hits == 1
            && !hook_error
            && consumer_entered
            && producer_entered
            && hook_ns <= entered_ns
            && entered_ns <= observer_ns
            && observation_ns >= 50_000_000
            && (!mode.selected() || (registered_arms == 3 && wake_consumed));
        let completed = consumer_done && producer_done && waker_done && hook_returned;
        let stall = !consumer_done
            && !producer_done
            && !hook_returned
            && consumer_state.state == State::Ready
            && producer_state.state == State::Ready
            && consumer_state.priority == mode.receiver_priority()
            && producer_state.priority == mode.sender_priority();
        Ok(Report {
            sender,
            receiver,
            waker,
            observer,
            hook_hits,
            hook_returned,
            hook_error,
            consumer_entered,
            producer_entered,
            consumer_done,
            producer_done,
            waker_done,
            consumer_state,
            producer_state,
            waker_state,
            hook_ns,
            entered_ns,
            observer_ns,
            observation_ns,
            gates_verified: true,
            select_verified: mode.selected(),
            registered_arms,
            wake_consumed,
            recovery: false,
            delivered: false,
            sent: 0,
            received: 0,
            stall: common && stall,
            completed: common && completed,
            success: common && (stall || completed),
        })
    })();

    // Quiesce all threads before clearing the callback's borrowed gate pointer.
    // Every error path keeps the independently runnable watchdog alive.
    let recovered = recover(mode, &shared);
    if recovered.is_err() {
        crate::fatal("priority-recovery-failed");
    }
    if observed.as_ref().map_or(true, |report| !report.success) {
        cancel(
            &shared,
            mode,
            &stop_sender,
            &[&producer_start, &consumer_start, &waker_start],
        );
    }
    let joined_producer = crate::join_bounded(producer);
    let joined_consumer = crate::join_bounded(consumer);
    let joined_waker = waker.map(crate::join_bounded).unwrap_or(Ok(()));
    HOOK_STAGE.store(0, Ordering::Release);
    HOOK_GATE.store(std::ptr::null_mut(), Ordering::Release);
    SELECT_OWNER.store(-1, Ordering::Release);
    drop(stop_sender);
    let mut report = observed?;
    joined_producer?;
    joined_consumer?;
    joined_waker?;
    report.recovery = recovered?;
    report.sent = shared.sent.load(Ordering::Acquire);
    report.received = shared.received.load(Ordering::Acquire);
    report.delivered = shared.valid.load(Ordering::Acquire)
        && shared.sender.done()
        && shared.receiver.done()
        && (!mode.selected() || shared.waker.done())
        && report.sent as usize == mode.messages()
        && report.received as usize == mode.messages();
    report.success &= report.delivered
        && !HOOK_ERROR.load(Ordering::Acquire)
        && ((report.completed && !report.recovery) || (report.stall && report.recovery));
    Ok(report)
}

pub fn host() -> Result<Report> {
    let schedule = native::schedule(native::tid())?;
    let (tx, rx) = bounded(CAPACITY);
    let consumer = thread::spawn(move || {
        let value: Message = rx
            .recv_timeout(Duration::from_millis(WAIT_MS.into()))
            .map_err(|_| "host-direct-receive")?;
        if !value.valid(1) {
            return Err("host-direct-payload");
        }
        native::schedule(native::tid())
    });
    let sent = tx.send(Message::new(1)).map_err(|_| "host-direct-send");
    let joined = crate::join_bounded(consumer);
    sent?;
    let receiver_schedule = joined?;
    let (stop_tx, stop_rx) = bounded(1);
    let (important_tx, important_rx) = bounded(CAPACITY);
    let (ordinary_tx, ordinary_rx) = bounded(CAPACITY);
    let consumer = thread::spawn(
        move || match select(&stop_rx, &important_rx, &ordinary_rx)? {
            Selected::Important(message) if message.valid(2) => Ok(()),
            _ => Err("host-selected-payload"),
        },
    );
    let sent = important_tx
        .send(Message::new(2))
        .map_err(|_| "host-selected-send");
    let joined = crate::join_bounded(consumer);
    sent?;
    joined?;
    drop((stop_tx, ordinary_tx));
    let (tx, rx) = bounded(CAPACITY);
    for sequence in 3..=10 {
        tx.send(Message::new(sequence))
            .map_err(|_| "host-ring-fill")?;
    }
    for sequence in 3..=10 {
        if !rx
            .try_recv()
            .map_err(|_| "host-ring-drain")?
            .valid(sequence)
        {
            return Err("host-ring-payload");
        }
    }
    Ok(Report {
        sender: schedule,
        receiver: receiver_schedule,
        observer: schedule,
        waker: Schedule {
            policy: -1,
            priority: -1,
        },
        hook_hits: 0,
        hook_returned: false,
        hook_error: false,
        consumer_entered: true,
        producer_entered: true,
        consumer_done: true,
        producer_done: true,
        waker_done: true,
        consumer_state: Snapshot::unavailable(),
        producer_state: Snapshot::unavailable(),
        waker_state: Snapshot::unavailable(),
        hook_ns: 0,
        entered_ns: 0,
        observer_ns: 0,
        observation_ns: 0,
        gates_verified: false,
        select_verified: false,
        registered_arms: 0,
        wake_consumed: false,
        recovery: false,
        delivered: true,
        sent: 10,
        received: 10,
        stall: false,
        completed: false,
        success: true,
    })
}

impl Report {
    pub fn print(self, run_id: &str, mode: Mode) -> Result<()> {
        let outcome = if !self.success {
            "invalid-observation"
        } else if mode == Mode::Host {
            "host-functional-only"
        } else if self.stall {
            "stall-observed"
        } else {
            "completed-without-repair"
        };
        let completed = self.success && self.completed && !self.recovery;
        println!(concat!("CHANNEL_PROGRESS {{\"schema\":\"channel-progress-v1\",\"record\":\"result\",",
            "\"run_id\":\"{}\",\"success\":{},\"outcome\":\"{}\",\"threads_joined\":true,\"watchdog_joined\":true,\"deadline_hit\":false,",
            "\"hook_hit_count\":{},\"hook_returned_before_recovery\":{},\"hook_error\":{},",
            "\"consumer_entered\":{},\"producer_entered\":{},\"consumer_done_before_recovery\":{},\"producer_done_before_recovery\":{},\"waker_done_before_recovery\":{},",
            "\"consumer_state_at_deadline\":\"{}\",\"producer_state_at_deadline\":\"{}\",\"waker_state_at_deadline\":\"{}\",",
            "\"consumer_priority_at_deadline\":{},\"producer_priority_at_deadline\":{},\"waker_priority_at_deadline\":{},",
            "\"hook_timestamp_ns\":{},\"operation_enter_timestamp_ns\":{},\"observer_timestamp_ns\":{},\"observation_ns\":{},",
            "\"initial_gate_waits_verified\":{},\"initial_select_wait_verified\":{},\"selection_registered_arms\":{},\"independent_wake_consumed\":{},\"selected_target_arm\":\"{}\",",
            "\"recovery_applied\":{},\"delivered\":{},\"messages_sent\":{},\"messages_received\":{},",
            "\"reservation_stall_observed\":{},\"completed_without_repair\":{},\"progress_contract_satisfied\":{},\"priority_progress_qualified\":false,\"timing_qualified\":false}}"),
            run_id, self.success, outcome, self.hook_hits, self.hook_returned, self.hook_error,
            self.consumer_entered, self.producer_entered, self.consumer_done, self.producer_done, self.waker_done,
            self.consumer_state.state.label(), self.producer_state.state.label(), self.waker_state.state.label(),
            self.consumer_state.priority, self.producer_state.priority, self.waker_state.priority,
            self.hook_ns, self.entered_ns, self.observer_ns, self.observation_ns,
            self.gates_verified, self.select_verified, self.registered_arms, self.wake_consumed,
            if mode.selected() { "important" } else { "none" },
            self.recovery, self.delivered, self.sent, self.received, self.stall, completed, completed);
        if self.success {
            Ok(())
        } else {
            Err("progress-interleaving-not-established")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cancellation_wakes_the_channel_wait_and_releases_native_gates() {
        let shared = Arc::new(Shared::new(0));
        let ready = Gate::new().unwrap();
        let cleanup_gate = Gate::new().unwrap();
        let (stop_tx, stop_rx) = bounded(1);
        let (important_tx, important_rx) = bounded(CAPACITY);
        let (ordinary_tx, ordinary_rx) = bounded(CAPACITY);
        let (worker, ready_worker) = (shared.clone(), ready.clone());
        let handle = thread::spawn(move || {
            ready_worker.post()?;
            match select(&stop_rx, &important_rx, &ordinary_rx)? {
                Selected::Stop if worker.cancelled.load(Ordering::Acquire) => Ok(()),
                _ => Err("cancellation-did-not-wake-selected-owner"),
            }
        });
        ready.wait(WAIT_MS).unwrap();
        // This host test exercises the real cancellation wake regardless of the
        // entry race. Only the target TCB/source witness proves actual parking.
        cancel(&shared, Mode::SelectReserved, &stop_tx, &[&cleanup_gate]);
        let joined = crate::join_bounded(handle);
        cleanup_gate.wait(WAIT_MS).unwrap();
        joined.unwrap();
        drop((important_tx, ordinary_tx));
    }
}

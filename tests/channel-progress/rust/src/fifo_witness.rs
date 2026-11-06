//! A narrow equal-FIFO, run-to-block scheduling experiment, not a transport.
//!
//! Both channel participants retain their original priority. A finite observer
//! turn makes the peer READY at an actual slot boundary, then blocks. Native
//! state and source markers must show that the original holder resumes and
//! commits before the peer accesses the slot. No retry or scheduling repair is
//! allowed to turn a failed observation into a successful result.
use crate::{
    join_bounded,
    native::{self, Gate, Result, Schedule, State},
    wait_until, Message, Watchdog, CAPACITY, HIGH_PRIORITY, OBSERVER_PRIORITY, RUN_BUDGET_MS,
    WAIT_MS, WORKER_STACK,
};
use crossbeam_channel::{bounded, Receiver, Sender, TrySendError};
use std::{
    ffi::c_void,
    fmt::{Display, Write},
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicPtr, AtomicU32, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

const SEND_RESERVED: u32 = 0x301;
const SEND_PUBLISHED: u32 = 0x302;
const RECV_RESERVED: u32 = 0x303;
const RECV_RECLAIMED: u32 = 0x304;
const SELECT_RESUMED: u32 = 0x200;
const SELECT_TIMEOUT_MS: u32 = 200;
const INTERFERENCE_ITERATIONS: u32 = 2_000_000;
const INTERFERENCE_MS: u32 = 1000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Case {
    ReceiveReserved,
    ReceivePublished,
    SelectReserved,
    SelectPublished,
    SendReserved,
    SendReclaimed,
}
impl Case {
    fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "fifo-try-recv-reserved" => Self::ReceiveReserved,
            "fifo-try-recv-published" => Self::ReceivePublished,
            "fifo-select-reserved" => Self::SelectReserved,
            "fifo-select-published" => Self::SelectPublished,
            "fifo-try-send-reserved" => Self::SendReserved,
            "fifo-try-send-reclaimed" => Self::SendReclaimed,
            _ => return None,
        })
    }
    fn label(self) -> &'static str {
        match self {
            Self::ReceiveReserved => "fifo-try-recv-reserved",
            Self::ReceivePublished => "fifo-try-recv-published",
            Self::SelectReserved => "fifo-select-reserved",
            Self::SelectPublished => "fifo-select-published",
            Self::SendReserved => "fifo-try-send-reserved",
            Self::SendReclaimed => "fifo-try-send-reclaimed",
        }
    }
    fn selected(self) -> bool {
        matches!(self, Self::SelectReserved | Self::SelectPublished)
    }
    fn reverse(self) -> bool {
        matches!(self, Self::SendReserved | Self::SendReclaimed)
    }
    fn reserved(self) -> bool {
        matches!(
            self,
            Self::ReceiveReserved | Self::SelectReserved | Self::SendReserved
        )
    }
    fn trigger(self) -> u32 {
        match self {
            Self::ReceiveReserved | Self::SelectReserved => SEND_RESERVED,
            Self::ReceivePublished | Self::SelectPublished => SEND_PUBLISHED,
            Self::SendReserved => RECV_RESERVED,
            Self::SendReclaimed => RECV_RECLAIMED,
        }
    }
    fn commit(self) -> u32 {
        if self.reverse() {
            RECV_RECLAIMED
        } else {
            SEND_PUBLISHED
        }
    }
    fn messages(self) -> u32 {
        if self.reverse() {
            CAPACITY as u32 + 1
        } else {
            1
        }
    }
    fn holder_api(self) -> &'static str {
        if self.reverse() {
            "try_recv"
        } else {
            "try_send"
        }
    }
    fn peer_api(self) -> &'static str {
        if self.selected() {
            "select_biased"
        } else if self.reverse() {
            "try_send"
        } else {
            "try_recv"
        }
    }
}

pub fn recognizes(value: &str) -> bool {
    Case::parse(value).is_some()
}

// Fixed-size, allocation-free source callback storage. Only the primary pair
// of channel operations is counted; the reverse prefill and final finite drain
// are outside that window. No selected token escapes the receive-only macro.
#[derive(Clone, Copy)]
#[repr(usize)]
enum Event {
    HolderEnter,
    PeerEnter,
    SelectRegistered,
    SelectWaitVerified,
    HolderReserve,
    HolderCommit,
    Trigger,
    ObserverTrigger,
    PeerReady,
    ObserverBlock,
    HolderResume,
    HolderReturn,
    PeerReserve,
    PeerCommit,
    PeerWaitResume,
    PeerReturn,
}
const EVENTS: [&str; 16] = [
    "holder_enter_order",
    "peer_enter_order",
    "select_registered_order",
    "select_wait_verified_order",
    "holder_reserve_order",
    "holder_commit_order",
    "trigger_order",
    "observer_trigger_order",
    "peer_ready_order",
    "observer_block_order",
    "holder_resume_order",
    "holder_return_order",
    "peer_reserve_order",
    "peer_commit_order",
    "peer_wait_resume_order",
    "peer_return_order",
];
#[derive(Clone, Copy)]
#[repr(usize)]
enum Count {
    Trigger,
    HolderResume,
    HolderReserved,
    HolderCommitted,
    PeerReserved,
    PeerCommitted,
    SelectRegistration,
    SelectWaitResume,
}
const COUNTS: [&str; 8] = [
    "trigger_count",
    "holder_resume_count",
    "holder_reserved_count",
    "holder_committed_count",
    "peer_reserved_count",
    "peer_committed_count",
    "select_registration_count",
    "select_wait_resume_count",
];
static ACTIVE: AtomicBool = AtomicBool::new(false);
static PRIMARY: AtomicBool = AtomicBool::new(false);
static NEXT_ORDER: AtomicU32 = AtomicU32::new(0);
static ORDERS: [AtomicU32; EVENTS.len()] = [const { AtomicU32::new(0) }; EVENTS.len()];
static HITS: [AtomicU32; COUNTS.len()] = [const { AtomicU32::new(0) }; COUNTS.len()];
static HOLDER_TID: AtomicI32 = AtomicI32::new(-1);
static PEER_TID: AtomicI32 = AtomicI32::new(-1);
static OBSERVER_TID: AtomicI32 = AtomicI32::new(-1);
static TRIGGER_STAGE: AtomicU32 = AtomicU32::new(0);
static HOLDER_COMMIT_STAGE: AtomicU32 = AtomicU32::new(0);
static SELECTED: AtomicBool = AtomicBool::new(false);
static REGISTERED_ARMS: AtomicU32 = AtomicU32::new(0);
static HOOK_ERROR: AtomicBool = AtomicBool::new(false);
static HOOK_GATE: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());
static OBSERVER_STATE: AtomicU32 = AtomicU32::new(0);
static OBSERVER_PRIORITY_SEEN: AtomicI32 = AtomicI32::new(-1);

fn read(value: &AtomicU32) -> u32 {
    value.load(Ordering::Acquire)
}
fn flag(value: &AtomicBool) -> bool {
    value.load(Ordering::Acquire)
}
fn mark(event: Event) {
    let order = NEXT_ORDER.fetch_add(1, Ordering::AcqRel) + 1;
    if ORDERS[event as usize]
        .compare_exchange(0, order, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        HOOK_ERROR.store(true, Ordering::Release);
    }
}
fn count(kind: Count) -> u32 {
    HITS[kind as usize].fetch_add(1, Ordering::AcqRel) + 1
}
fn hits(kind: Count) -> u32 {
    read(&HITS[kind as usize])
}

#[derive(Clone, Copy)]
struct Snapshot {
    state: State,
    priority: i32,
}
impl Default for Snapshot {
    fn default() -> Self {
        Self {
            state: State::Unavailable,
            priority: -1,
        }
    }
}
impl Snapshot {
    fn read(tid: i32) -> Self {
        let (state, priority) = native::state(tid);
        Self { state, priority }
    }
    fn is(self, state: State, priority: i32) -> bool {
        self.state == state && self.priority == priority
    }
    fn code(self) -> u32 {
        match self.state {
            State::Unavailable => 0,
            State::WaitSem => 1,
            State::Ready => 2,
            State::Running => 3,
            State::Other => 4,
            State::Absent => 5,
        }
    }
    fn observer() -> Self {
        Self {
            state: match read(&OBSERVER_STATE) {
                1 => State::WaitSem,
                2 => State::Ready,
                3 => State::Running,
                4 => State::Other,
                5 => State::Absent,
                _ => State::Unavailable,
            },
            priority: OBSERVER_PRIORITY_SEEN.load(Ordering::Acquire),
        }
    }
}

pub fn active() -> bool {
    flag(&ACTIVE)
}

pub fn hook(stage: u32) {
    if !flag(&PRIMARY) {
        return;
    }
    let tid = native::tid();
    let holder = tid == HOLDER_TID.load(Ordering::Acquire);
    let peer = tid == PEER_TID.load(Ordering::Acquire);
    if !holder && !peer {
        return;
    }
    if peer && flag(&SELECTED) && (0x100..0x200).contains(&stage) {
        REGISTERED_ARMS.store(stage - 0x100, Ordering::Release);
        count(Count::SelectRegistration);
        mark(Event::SelectRegistered);
        return;
    }
    if peer && flag(&SELECTED) && stage == SELECT_RESUMED {
        count(Count::SelectWaitResume);
        mark(Event::PeerWaitResume);
        return;
    }
    let holder_commit = read(&HOLDER_COMMIT_STAGE);
    let commit = if holder {
        holder_commit
    } else {
        SEND_PUBLISHED + RECV_RECLAIMED - holder_commit
    };
    if stage == commit - 1 {
        count(if holder {
            Count::HolderReserved
        } else {
            Count::PeerReserved
        });
        mark(if holder {
            Event::HolderReserve
        } else {
            Event::PeerReserve
        });
    } else if stage == commit {
        count(if holder {
            Count::HolderCommitted
        } else {
            Count::PeerCommitted
        });
        mark(if holder {
            Event::HolderCommit
        } else {
            Event::PeerCommit
        });
    } else {
        HOOK_ERROR.store(true, Ordering::Release);
        return;
    }
    if !holder || stage != read(&TRIGGER_STAGE) {
        return;
    }
    if count(Count::Trigger) != 1 {
        HOOK_ERROR.store(true, Ordering::Release);
        return;
    }
    mark(Event::Trigger);
    let gate = HOOK_GATE.load(Ordering::Acquire);
    // The gate is owned until both joins. Posting preempts to the observer; the
    // hook never waits, yields, allocates or changes any scheduling policy.
    if gate.is_null() || unsafe { native::scp_gate_post(gate) } != 0 {
        HOOK_ERROR.store(true, Ordering::Release);
    }
    let observer = Snapshot::read(OBSERVER_TID.load(Ordering::Acquire));
    OBSERVER_PRIORITY_SEEN.store(observer.priority, Ordering::Relaxed);
    OBSERVER_STATE.store(observer.code(), Ordering::Release);
    count(Count::HolderResume);
    mark(Event::HolderResume);
}

struct HookScope;
impl HookScope {
    fn new(case: Case, gate: &Gate) -> Result<Self> {
        if ACTIVE
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err("fifo-overlapping-witness");
        }
        PRIMARY.store(false, Ordering::Release);
        NEXT_ORDER.store(0, Ordering::Relaxed);
        for value in ORDERS.iter().chain(HITS.iter()) {
            value.store(0, Ordering::Relaxed);
        }
        HOLDER_TID.store(-1, Ordering::Relaxed);
        PEER_TID.store(-1, Ordering::Relaxed);
        OBSERVER_TID.store(native::tid(), Ordering::Relaxed);
        TRIGGER_STAGE.store(case.trigger(), Ordering::Relaxed);
        HOLDER_COMMIT_STAGE.store(case.commit(), Ordering::Relaxed);
        SELECTED.store(case.selected(), Ordering::Relaxed);
        REGISTERED_ARMS.store(0, Ordering::Relaxed);
        HOOK_ERROR.store(false, Ordering::Relaxed);
        OBSERVER_STATE.store(0, Ordering::Relaxed);
        OBSERVER_PRIORITY_SEEN.store(-1, Ordering::Relaxed);
        HOOK_GATE.store(gate.pointer(), Ordering::Release);
        Ok(Self)
    }
}
impl Drop for HookScope {
    fn drop(&mut self) {
        // All threads have joined before this lifetime barrier is released.
        PRIMARY.store(false, Ordering::Release);
        HOOK_GATE.store(std::ptr::null_mut(), Ordering::Release);
        ACTIVE.store(false, Ordering::Release);
    }
}

struct OwnedMessage(Message);
impl OwnedMessage {
    fn new(sequence: u32) -> Self {
        Self(Message::new(sequence))
    }
    fn valid(&self, sequence: u32) -> bool {
        self.0.valid(sequence)
    }
}

struct Channels {
    sender: Sender<OwnedMessage>,
    receiver: Receiver<OwnedMessage>,
    stop_sender: Sender<()>,
    stop_receiver: Receiver<()>,
    ordinary_sender: Sender<OwnedMessage>,
    ordinary_receiver: Receiver<OwnedMessage>,
}
impl Channels {
    fn new() -> Self {
        let (sender, receiver) = bounded(CAPACITY);
        let (stop_sender, stop_receiver) = bounded(1);
        let (ordinary_sender, ordinary_receiver) = bounded(CAPACITY);
        Self {
            sender,
            receiver,
            stop_sender,
            stop_receiver,
            ordinary_sender,
            ordinary_receiver,
        }
    }
}
enum Selected {
    Stop,
    Important(OwnedMessage),
    Ordinary(OwnedMessage),
    Timeout,
}
fn select_once(channels: &Channels, timeout: Duration) -> Result<Selected> {
    // Only receive arms; the macro completes the reserved token immediately.
    crossbeam_channel::select_biased! {
        recv(channels.stop_receiver) -> value => {
            value.map_err(|_| "fifo-stop-disconnected")?;
            Ok(Selected::Stop)
        },
        recv(channels.receiver) -> value => value.map(Selected::Important).map_err(|_| "fifo-important-disconnected"),
        recv(channels.ordinary_receiver) -> value => value.map(Selected::Ordinary).map_err(|_| "fifo-ordinary-disconnected"),
        default(timeout) => Ok(Selected::Timeout),
    }
}

#[derive(Default)]
struct Worker {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    calls: AtomicU32,
    primary_done: AtomicBool,
    done: AtomicBool,
}
impl Worker {
    fn setup(&self) -> Result<()> {
        let tid = native::tid();
        self.tid.store(tid, Ordering::Release);
        let actual = native::fifo(tid, HIGH_PRIORITY)?;
        self.policy.store(actual.policy, Ordering::Relaxed);
        self.priority.store(actual.priority, Ordering::Release);
        Ok(())
    }
    fn schedule(&self) -> Schedule {
        Schedule {
            policy: self.policy.load(Ordering::Acquire),
            priority: self.priority.load(Ordering::Acquire),
        }
    }
    fn snapshot(&self) -> Snapshot {
        Snapshot::read(self.tid.load(Ordering::Acquire))
    }
}
struct Shared {
    holder: Worker,
    peer: Worker,
    cancelled: AtomicBool,
    payload_valid: AtomicBool,
    sent: AtomicU32,
    received: AtomicU32,
    drained: AtomicU32,
    select_outcome: AtomicU32,
    select_timeouts: AtomicU32,
}
impl Shared {
    fn new() -> Self {
        Self {
            holder: Worker::default(),
            peer: Worker::default(),
            cancelled: AtomicBool::new(false),
            payload_valid: AtomicBool::new(true),
            sent: AtomicU32::new(0),
            received: AtomicU32::new(0),
            drained: AtomicU32::new(0),
            select_outcome: AtomicU32::new(0),
            select_timeouts: AtomicU32::new(0),
        }
    }
    fn send(&self, sender: &Sender<OwnedMessage>, sequence: u32) -> Result<()> {
        sender
            .try_send(OwnedMessage::new(sequence))
            .map_err(|_| "fifo-single-send-failed")?;
        self.sent.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
    fn receive(&self, value: OwnedMessage, sequence: u32) -> Result<()> {
        self.received.fetch_add(1, Ordering::Relaxed);
        if !value.valid(sequence) {
            self.payload_valid.store(false, Ordering::Release);
            return Err("fifo-payload-mismatch");
        }
        Ok(())
    }
    fn take(&self, receiver: &Receiver<OwnedMessage>, sequence: u32) -> Result<()> {
        self.receive(
            receiver
                .try_recv()
                .map_err(|_| "fifo-single-receive-failed")?,
            sequence,
        )
    }
    fn selected(&self, channels: &Channels) -> Result<()> {
        match select_once(channels, Duration::from_millis(SELECT_TIMEOUT_MS.into()))? {
            Selected::Important(value) => {
                self.select_outcome.store(1, Ordering::Release);
                self.receive(value, 1)
            }
            Selected::Ordinary(value) => {
                self.select_outcome.store(2, Ordering::Release);
                // Keep ownership explicit even for an invalid arm outcome.
                self.receive(value, 1)?;
                Err("fifo-selected-ordinary")
            }
            Selected::Stop => {
                self.select_outcome.store(3, Ordering::Release);
                Err("fifo-selected-stop")
            }
            Selected::Timeout => {
                self.select_outcome.store(4, Ordering::Release);
                self.select_timeouts.fetch_add(1, Ordering::Relaxed);
                Err("fifo-selected-timeout")
            }
        }
    }
    fn outcome(&self) -> &'static str {
        match read(&self.select_outcome) {
            1 => "important",
            2 => "ordinary",
            3 => "stop",
            4 => "timeout",
            _ => "none",
        }
    }
}

struct Gates {
    ready: Arc<Gate>,
    holder_start: Arc<Gate>,
    peer_start: Arc<Gate>,
    hook: Arc<Gate>,
    completed: Arc<Gate>,
    drain_start: Arc<Gate>,
    holder_finish: Arc<Gate>,
    peer_finish: Arc<Gate>,
}
impl Gates {
    fn new() -> Result<Self> {
        Ok(Self {
            ready: Gate::new()?,
            holder_start: Gate::new()?,
            peer_start: Gate::new()?,
            hook: Gate::new()?,
            completed: Gate::new()?,
            drain_start: Gate::new()?,
            holder_finish: Gate::new()?,
            peer_finish: Gate::new()?,
        })
    }
    fn cancel(&self, shared: &Shared, channels: &Channels) -> Result<()> {
        PRIMARY.store(false, Ordering::Release);
        shared.cancelled.store(true, Ordering::Release);
        // This unused, independent stop queue also wakes a real selected wait.
        // Cancellation invalidates the observation; it is never progress proof.
        let stop = match channels.stop_sender.try_send(()) {
            Ok(()) | Err(TrySendError::Disconnected(())) => Ok(()),
            Err(TrySendError::Full(())) => Err("fifo-cancellation-stop-full"),
        };
        let mut release = Ok(());
        for gate in [
            &self.holder_start,
            &self.peer_start,
            &self.drain_start,
            &self.holder_finish,
            &self.peer_finish,
        ] {
            if let Err(error) = gate.post() {
                release = Err(error);
            }
        }
        stop.and(release)
    }
}

fn worker(
    case: Case,
    holder: bool,
    shared: &Shared,
    channels: &Channels,
    gates: &Gates,
) -> Result<()> {
    let actor = if holder { &shared.holder } else { &shared.peer };
    let setup = actor.setup().and_then(|()| {
        if case.reverse() && !holder {
            // The same 180/FIFO sender owns the finite prefill, outside PRIMARY.
            for sequence in 1..=CAPACITY as u32 {
                shared.send(&channels.sender, sequence)?;
            }
        }
        Ok(())
    });
    gates.ready.post()?;
    let primary = setup.and_then(|()| {
        let start = if holder {
            &gates.holder_start
        } else {
            &gates.peer_start
        };
        start.wait(RUN_BUDGET_MS)?;
        if flag(&shared.cancelled) {
            return Err("fifo-worker-cancelled");
        }
        actor.calls.fetch_add(1, Ordering::Relaxed);
        mark(if holder {
            Event::HolderEnter
        } else {
            Event::PeerEnter
        });
        let operation = if holder {
            if case.reverse() {
                shared.take(&channels.receiver, 1)
            } else {
                shared.send(&channels.sender, 1)
            }
        } else if case.selected() {
            shared.selected(channels)
        } else if case.reverse() {
            shared.send(&channels.sender, CAPACITY as u32 + 1)
        } else {
            shared.take(&channels.receiver, 1)
        };
        mark(if holder {
            Event::HolderReturn
        } else {
            Event::PeerReturn
        });
        operation
    });
    actor.primary_done.store(true, Ordering::Release);
    gates.completed.post()?;
    let drain = if holder && case.reverse() && primary.is_ok() {
        gates.drain_start.wait(RUN_BUDGET_MS).and_then(|()| {
            if flag(&shared.cancelled) {
                return Err("fifo-drain-cancelled");
            }
            // A second explicitly bounded turn, after both primary operations.
            for sequence in 2..=CAPACITY as u32 + 1 {
                shared.take(&channels.receiver, sequence)?;
                shared.drained.fetch_add(1, Ordering::Relaxed);
            }
            Ok(())
        })
    } else {
        Ok(())
    };
    actor.done.store(true, Ordering::Release);
    // No endpoint/TLS teardown can influence the primary source-order proof.
    let finish = if holder {
        &gates.holder_finish
    } else {
        &gates.peer_finish
    };
    let held = finish.wait(RUN_BUDGET_MS);
    primary.and(drain).and(held)
}

fn spawn(
    case: Case,
    holder: bool,
    shared: Arc<Shared>,
    channels: Arc<Channels>,
    gates: Arc<Gates>,
) -> Result<JoinHandle<Result<()>>> {
    thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || worker(case, holder, &shared, &channels, &gates))
        .map_err(|_| "fifo-worker-spawn")
}

#[derive(Default)]
struct Observation {
    initial_gates: bool,
    select_wait: bool,
    holder_trigger: Snapshot,
    peer_trigger: Snapshot,
    holder_ready: Snapshot,
    peer_ready: Snapshot,
    hook_returned_at_ready: bool,
    commit_at_ready: u32,
    iterations: u32,
    elapsed_ns: u64,
    final_waits: bool,
}
fn schedule_is(schedule: Schedule, priority: i32) -> bool {
    schedule.policy == 1 && schedule.priority == priority
}

fn observe(
    case: Case,
    shared: &Shared,
    gates: &Gates,
    observation: &mut Observation,
) -> Result<()> {
    gates.ready.wait(WAIT_MS)?;
    gates.ready.wait(WAIT_MS)?;
    wait_until(|| {
        shared.holder.snapshot().is(State::WaitSem, HIGH_PRIORITY)
            && shared.peer.snapshot().is(State::WaitSem, HIGH_PRIORITY)
    })?;
    if !schedule_is(shared.holder.schedule(), HIGH_PRIORITY)
        || !schedule_is(shared.peer.schedule(), HIGH_PRIORITY)
    {
        return Err("fifo-initial-policy");
    }
    observation.initial_gates = true;
    HOLDER_TID.store(shared.holder.tid.load(Ordering::Acquire), Ordering::Relaxed);
    PEER_TID.store(shared.peer.tid.load(Ordering::Acquire), Ordering::Relaxed);
    PRIMARY.store(true, Ordering::Release);
    if case.selected() {
        gates.peer_start.post()?;
        wait_until(|| {
            read(&REGISTERED_ARMS) == 3
                && hits(Count::SelectRegistration) == 1
                && shared.peer.snapshot().is(State::WaitSem, HIGH_PRIORITY)
        })?;
        if hits(Count::SelectWaitResume) != 0 {
            return Err("fifo-select-resumed-during-setup");
        }
        observation.select_wait = true;
        mark(Event::SelectWaitVerified);
    }
    gates.holder_start.post()?;
    gates.hook.wait(WAIT_MS)?;
    mark(Event::ObserverTrigger);
    observation.holder_trigger = shared.holder.snapshot();
    observation.peer_trigger = shared.peer.snapshot();
    if !observation.holder_trigger.is(State::Ready, HIGH_PRIORITY)
        || !observation.peer_trigger.is(State::WaitSem, HIGH_PRIORITY)
        || hits(Count::Trigger) != 1
        || hits(Count::HolderResume) != 0
        || hits(Count::SelectWaitResume) != 0
    {
        return Err("fifo-trigger-native-state");
    }
    if !case.selected() {
        gates.peer_start.post()?;
    }

    // Stay runnable at 220 until the independent native timeout makes the
    // selected owner READY. There are no sleeps, waits, yields or channel calls
    // in this finite turn. Both the work count and elapsed duration are capped.
    let start = native::monotonic_ns()?;
    loop {
        observation.iterations += 1;
        observation.peer_ready = shared.peer.snapshot();
        observation.elapsed_ns = native::monotonic_ns()?
            .checked_sub(start)
            .ok_or("fifo-clock-regressed")?;
        if observation.elapsed_ns > u64::from(INTERFERENCE_MS) * 1_000_000 {
            return Err("fifo-interference-time-limit");
        }
        if observation.peer_ready.is(State::Ready, HIGH_PRIORITY) {
            break;
        }
        if observation.iterations >= INTERFERENCE_ITERATIONS {
            return Err("fifo-interference-work-limit");
        }
    }
    observation.holder_ready = shared.holder.snapshot();
    observation.hook_returned_at_ready = hits(Count::HolderResume) != 0;
    observation.commit_at_ready = hits(Count::HolderCommitted);
    mark(Event::PeerReady);
    if !observation.holder_ready.is(State::Ready, HIGH_PRIORITY)
        || observation.hook_returned_at_ready
        || observation.commit_at_ready != u32::from(!case.reserved())
        || (case.selected() && hits(Count::SelectWaitResume) != 0)
        || (!case.selected() && read(&shared.peer.calls) != 0)
    {
        return Err("fifo-ready-before-resume-proof");
    }
    mark(Event::ObserverBlock);
    // This separate completion gate is empty: neither primary operation has
    // returned. The resumed holder itself must sample our actual WAIT_SEM.
    gates.completed.wait(WAIT_MS)?;
    gates.completed.wait(WAIT_MS)?;
    if !flag(&shared.holder.primary_done) || !flag(&shared.peer.primary_done) {
        return Err("fifo-primary-completion");
    }
    PRIMARY.store(false, Ordering::Release);
    if case.reverse() {
        gates.drain_start.post()?;
    }
    wait_until(|| {
        flag(&shared.holder.done)
            && flag(&shared.peer.done)
            && shared.holder.snapshot().is(State::WaitSem, HIGH_PRIORITY)
            && shared.peer.snapshot().is(State::WaitSem, HIGH_PRIORITY)
    })?;
    if !schedule_is(
        native::schedule(shared.holder.tid.load(Ordering::Acquire))?,
        HIGH_PRIORITY,
    ) || !schedule_is(
        native::schedule(shared.peer.tid.load(Ordering::Acquire))?,
        HIGH_PRIORITY,
    ) {
        return Err("fifo-final-policy");
    }
    observation.final_waits = true;
    Ok(())
}

struct Trace {
    orders: [u32; EVENTS.len()],
    counts: [u32; COUNTS.len()],
    registered_arms: u32,
    observer: Snapshot,
    hook_error: bool,
}
impl Trace {
    fn capture() -> Self {
        Self {
            orders: std::array::from_fn(|index| read(&ORDERS[index])),
            counts: std::array::from_fn(|index| read(&HITS[index])),
            registered_arms: read(&REGISTERED_ARMS),
            observer: Snapshot::observer(),
            hook_error: flag(&HOOK_ERROR),
        }
    }
    fn ordered(&self, events: &[Event]) -> bool {
        let values: Vec<_> = events
            .iter()
            .map(|event| self.orders[*event as usize])
            .collect();
        values.iter().all(|value| *value > 0) && values.windows(2).all(|pair| pair[0] < pair[1])
    }
    fn valid(&self, case: Case) -> bool {
        use Event::*;
        let distinct = self
            .orders
            .iter()
            .enumerate()
            .all(|(index, order)| *order == 0 || !self.orders[..index].contains(order));
        let counts = self.counts.iter().enumerate().all(|(index, count)| {
            *count
                == if index >= Count::SelectRegistration as usize {
                    u32::from(case.selected())
                } else {
                    1
                }
        });
        let boundary = if case.reserved() {
            HolderReserve
        } else {
            HolderCommit
        };
        let common = self.ordered(&[HolderEnter, HolderReserve, HolderCommit, HolderReturn])
            && self.ordered(&[
                boundary,
                Trigger,
                ObserverTrigger,
                PeerReady,
                ObserverBlock,
                HolderResume,
                HolderReturn,
            ])
            && (!case.reserved() || self.ordered(&[HolderResume, HolderCommit]));
        let peer = if case.selected() {
            self.ordered(&[PeerEnter, SelectRegistered, SelectWaitVerified, HolderEnter])
                && self.ordered(&[
                    HolderCommit,
                    PeerWaitResume,
                    PeerReserve,
                    PeerCommit,
                    PeerReturn,
                ])
                && self.ordered(&[HolderResume, PeerWaitResume])
        } else {
            [SelectRegistered, SelectWaitVerified, PeerWaitResume]
                .iter()
                .all(|event| self.orders[*event as usize] == 0)
                && self.ordered(&[HolderCommit, PeerEnter, PeerReserve, PeerCommit, PeerReturn])
                && self.ordered(&[HolderResume, PeerEnter])
        };
        distinct
            && counts
            && common
            && peer
            && !self.hook_error
            && self.registered_arms == if case.selected() { 3 } else { 0 }
            && self.observer.is(State::WaitSem, OBSERVER_PRIORITY)
    }
}

struct Report {
    observer: Schedule,
    shared: Arc<Shared>,
    observation: Observation,
    trace: Trace,
    failure: Option<&'static str>,
    cleanup_error: Option<&'static str>,
}
fn target_case(case: Case) -> Result<Report> {
    let observer = native::fifo(native::tid(), OBSERVER_PRIORITY)?;
    let channels = Arc::new(Channels::new());
    // Keep every endpoint alive through all joins, including the unused arm.
    let _ordinary_endpoint = &channels.ordinary_sender;
    let shared = Arc::new(Shared::new());
    let gates = Arc::new(Gates::new()?);
    let scope = HookScope::new(case, &gates.hook)?;
    let holder = spawn(case, true, shared.clone(), channels.clone(), gates.clone())?;
    let peer = match spawn(case, false, shared.clone(), channels.clone(), gates.clone()) {
        Ok(handle) => handle,
        Err(error) => {
            let cleanup = gates.cancel(&shared, &channels);
            let joined = join_bounded(holder);
            cleanup?;
            joined?;
            return Err(error);
        }
    };
    let mut observation = Observation::default();
    let observed = observe(case, &shared, &gates, &mut observation);
    PRIMARY.store(false, Ordering::Release);
    let trace = Trace::capture();
    let cleanup = if observed.is_err() || !trace.valid(case) {
        gates.cancel(&shared, &channels)
    } else {
        // Success performs no channel wake, worker priority change or recovery.
        let holder_release = gates.holder_finish.post();
        let peer_release = gates.peer_finish.post();
        holder_release.and(peer_release)
    };
    let holder_joined = join_bounded(holder);
    let peer_joined = join_bounded(peer);
    drop(scope);
    Ok(Report {
        observer,
        shared,
        observation,
        trace,
        failure: observed.err().or(holder_joined.err()).or(peer_joined.err()),
        cleanup_error: cleanup.err(),
    })
}

// Only fixed ASCII labels, decimal numbers and hexadecimal build identities
// enter these records; no user-controlled text is interpolated into JSON.
struct Record(String);
impl Record {
    fn new(kind: &str, run_id: &str, case: Case) -> Self {
        let mut value = Self(String::from(
            "CHANNEL_PROGRESS {\"schema\":\"fifo-channel-progress-v1\"",
        ));
        value.text("record", kind);
        value.text("run_id", run_id);
        value.text("mode", case.label());
        value
    }
    fn value(&mut self, key: &str, value: impl Display) {
        write!(&mut self.0, ",\"{key}\":{value}").expect("format fixed record");
    }
    fn text(&mut self, key: &str, value: &str) {
        self.value(key, format_args!("\"{value}\""));
    }
    fn schedule(&mut self, policy_key: &str, priority_key: &str, schedule: Schedule) {
        self.value(policy_key, schedule.policy);
        self.value(priority_key, schedule.priority);
    }
    fn snapshot(&mut self, state_key: &str, priority_key: &str, snapshot: Snapshot) {
        self.text(state_key, snapshot.state.label());
        self.value(priority_key, snapshot.priority);
    }
    fn print(mut self) {
        self.0.push('}');
        println!("{}", self.0);
    }
}
impl Report {
    fn valid(&self, case: Case, watchdog: Schedule) -> bool {
        let s = &self.shared;
        let o = &self.observation;
        self.failure.is_none()
            && self.cleanup_error.is_none()
            && self.trace.valid(case)
            && schedule_is(self.observer, OBSERVER_PRIORITY)
            && schedule_is(watchdog, crate::WATCHDOG_PRIORITY)
            && schedule_is(s.holder.schedule(), HIGH_PRIORITY)
            && schedule_is(s.peer.schedule(), HIGH_PRIORITY)
            && !flag(&s.cancelled)
            && flag(&s.payload_valid)
            && read(&s.sent) == case.messages()
            && read(&s.received) == case.messages()
            && read(&s.drained) == if case.reverse() { CAPACITY as u32 } else { 0 }
            && read(&s.holder.calls) == 1
            && read(&s.peer.calls) == 1
            && flag(&s.holder.done)
            && flag(&s.peer.done)
            && o.initial_gates
            && o.final_waits
            && o.select_wait == case.selected()
            && read(&s.select_timeouts) == 0
            && s.outcome() == if case.selected() { "important" } else { "none" }
            && o.holder_trigger.is(State::Ready, HIGH_PRIORITY)
            && o.peer_trigger.is(State::WaitSem, HIGH_PRIORITY)
            && o.holder_ready.is(State::Ready, HIGH_PRIORITY)
            && o.peer_ready.is(State::Ready, HIGH_PRIORITY)
            && !o.hook_returned_at_ready
            && o.commit_at_ready == u32::from(!case.reserved())
            && o.iterations > 0
            && o.iterations <= INTERFERENCE_ITERATIONS
            && o.elapsed_ns <= u64::from(INTERFERENCE_MS) * 1_000_000
    }
    fn print(&self, case: Case, run_id: &str, watchdog: Schedule) -> Result<()> {
        let s = &self.shared;
        let o = &self.observation;
        let success = self.valid(case, watchdog);
        let mut metadata = Record::new("metadata", run_id, case);
        for (key, value) in [
            ("build_id", env!("NXRS_CHANNEL_PROGRESS_BUILD_ID")),
            ("build_mode", "fifo-channel-suite"),
            ("contract", "equal-fifo-run-to-block-v1"),
            ("runtime", "nuttx"),
            ("transport", "crossbeam-channel"),
            ("transport_version", "0.5.17"),
            (
                "selection_bias",
                if case.selected() {
                    "stop-important-ordinary"
                } else {
                    "none"
                },
            ),
            ("holder_api", case.holder_api()),
            ("peer_api", case.peer_api()),
        ] {
            metadata.text(key, value);
        }
        for (key, value) in [
            ("hook_stage", 5),
            ("capacity", CAPACITY as u32),
            ("payload_bytes", 16),
            ("provider_count", 1),
            ("arm_count", if case.selected() { 3 } else { 1 }),
            ("trigger_stage", case.trigger()),
            ("commit_stage", case.commit()),
            ("participant_operation_budget", 1),
            (
                "drain_budget",
                if case.reverse() { CAPACITY as u32 } else { 0 },
            ),
            (
                "select_timeout_ms",
                if case.selected() {
                    SELECT_TIMEOUT_MS
                } else {
                    0
                },
            ),
            ("interference_iteration_limit", INTERFERENCE_ITERATIONS),
            ("interference_time_limit_ms", INTERFERENCE_MS),
        ] {
            metadata.value(key, value);
        }
        metadata.value("single_core_tcb_witness", native::is_nuttx());
        for key in [
            "async_channel_access",
            "manual_selected_operations",
            "selected_send",
            "endpoints_changed_during_window",
        ] {
            metadata.value(key, false);
        }
        metadata.schedule("holder_policy", "holder_priority", s.holder.schedule());
        metadata.schedule("peer_policy", "peer_priority", s.peer.schedule());
        metadata.schedule("observer_policy", "observer_priority", self.observer);
        metadata.schedule("watchdog_policy", "watchdog_priority", watchdog);
        metadata.print();

        let mut result = Record::new("result", run_id, case);
        result.text(
            "outcome",
            if success {
                "completed-without-repair"
            } else {
                "invalid-observation"
            },
        );
        for key in [
            "success",
            "diagnostic_valid",
            "completed_without_repair",
            "progress_contract_satisfied",
        ] {
            result.value(key, success);
        }
        for key in [
            "recovery_applied",
            "priority_progress_qualified",
            "production_transport_qualified",
            "timing_qualified",
        ] {
            result.value(key, false);
        }
        result.text("failure_reason", self.failure.unwrap_or("none"));
        result.text("cleanup_error", self.cleanup_error.unwrap_or("none"));
        result.value("hook_error", self.trace.hook_error);
        for (index, key) in COUNTS.iter().enumerate() {
            result.value(key, self.trace.counts[index]);
        }
        for (index, key) in EVENTS.iter().enumerate() {
            result.value(key, self.trace.orders[index]);
        }
        for (key, value) in [
            ("holder_operation_calls", read(&s.holder.calls)),
            ("peer_operation_calls", read(&s.peer.calls)),
            ("peer_retry_count", 0),
            ("messages_sent", read(&s.sent)),
            ("messages_received", read(&s.received)),
            ("holder_commit_count_at_ready", o.commit_at_ready),
            ("registered_arms", self.trace.registered_arms),
            ("select_timeout_count", read(&s.select_timeouts)),
            ("interference_iterations", o.iterations),
        ] {
            result.value(key, value);
        }
        for (key, value) in [
            ("payload_valid", flag(&s.payload_valid)),
            ("initial_gate_waits_verified", o.initial_gates),
            ("holder_hook_returned_at_ready", o.hook_returned_at_ready),
            ("initial_select_wait_verified", o.select_wait),
            ("holder_done", flag(&s.holder.done)),
            ("peer_done", flag(&s.peer.done)),
            ("final_native_waits_verified", o.final_waits),
            ("joins_complete", true),
            ("watchdog_complete", true),
        ] {
            result.value(key, value);
        }
        result.snapshot(
            "holder_state_at_trigger",
            "holder_priority_at_trigger",
            o.holder_trigger,
        );
        result.snapshot(
            "peer_state_at_trigger",
            "peer_priority_at_trigger",
            o.peer_trigger,
        );
        result.snapshot("peer_ready_state", "peer_ready_priority", o.peer_ready);
        result.snapshot(
            "holder_state_at_ready",
            "holder_priority_at_ready",
            o.holder_ready,
        );
        result.snapshot(
            "observer_state_at_holder_resume",
            "observer_priority_at_holder_resume",
            self.trace.observer,
        );
        result.text("select_outcome", s.outcome());
        result.value("interference_elapsed_ns", o.elapsed_ns);
        result.print();
        if success {
            Ok(())
        } else {
            Err("fifo-invalid-evidence")
        }
    }
}

pub fn run(value: &str) -> Result<()> {
    let case = Case::parse(value).ok_or("unknown-fifo-progress-case")?;
    if !native::is_nuttx() {
        return Err("fifo-witness-requires-single-core-flat-nuttx");
    }
    if env!("NXRS_CHANNEL_PROGRESS_HOOK_STAGE") != "5" {
        return Err("fifo-witness-requires-fifo-ordered-build");
    }
    let run_id = format!("{}-{}", native::tid(), native::monotonic_ns()?);
    let original = native::schedule(native::tid())?;
    let watchdog = Watchdog::start()?;
    let watchdog_schedule = watchdog.schedule;
    let report = target_case(case);
    // All worker joins precede this restoration of the observer's own policy.
    // The independent watchdog remains active until that lifetime barrier.
    let restore = native::set_schedule(native::tid(), original);
    let stopped = watchdog.finish();
    restore?;
    stopped?;
    report?.print(case, &run_id, watchdog_schedule)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cases_name_the_actual_pair_and_source_boundary() {
        for (name, trigger, commit, holder, peer) in [
            (
                "fifo-try-recv-reserved",
                0x301,
                0x302,
                "try_send",
                "try_recv",
            ),
            (
                "fifo-try-recv-published",
                0x302,
                0x302,
                "try_send",
                "try_recv",
            ),
            (
                "fifo-select-reserved",
                0x301,
                0x302,
                "try_send",
                "select_biased",
            ),
            (
                "fifo-select-published",
                0x302,
                0x302,
                "try_send",
                "select_biased",
            ),
            (
                "fifo-try-send-reserved",
                0x303,
                0x304,
                "try_recv",
                "try_send",
            ),
            (
                "fifo-try-send-reclaimed",
                0x304,
                0x304,
                "try_recv",
                "try_send",
            ),
        ] {
            let case = Case::parse(name).unwrap();
            assert_eq!(
                (
                    case.label(),
                    case.trigger(),
                    case.commit(),
                    case.holder_api(),
                    case.peer_api()
                ),
                (name, trigger, commit, holder, peer)
            );
        }
        assert!(!recognizes("select-reserved"));
        assert!(!recognizes("fifo-channel-suite"));
    }

    #[test]
    fn host_refuses_every_native_fifo_case_before_scheduler_setup() {
        if native::is_nuttx() {
            return;
        }
        for case in [
            Case::ReceiveReserved,
            Case::ReceivePublished,
            Case::SelectReserved,
            Case::SelectPublished,
            Case::SendReserved,
            Case::SendReclaimed,
        ] {
            assert_eq!(
                run(case.label()),
                Err("fifo-witness-requires-single-core-flat-nuttx")
            );
        }
    }

    #[test]
    fn owned_payloads_survive_full_admission_and_finite_reverse_drain() {
        assert_eq!(std::mem::size_of::<OwnedMessage>(), 16);
        let channels = Channels::new();
        let shared = Shared::new();
        for sequence in 1..=CAPACITY as u32 {
            shared.send(&channels.sender, sequence).unwrap();
        }
        let ninth = OwnedMessage::new(9);
        let Err(TrySendError::Full(ninth)) = channels.sender.try_send(ninth) else {
            panic!("expected owned full result");
        };
        assert!(ninth.valid(9));
        shared.take(&channels.receiver, 1).unwrap();
        assert!(channels.sender.try_send(ninth).is_ok());
        shared.sent.fetch_add(1, Ordering::Relaxed);
        for sequence in 2..=9 {
            shared.take(&channels.receiver, sequence).unwrap();
        }
        assert_eq!((read(&shared.sent), read(&shared.received)), (9, 9));
        assert!(channels.receiver.is_empty());
        assert!(flag(&shared.payload_valid));
    }

    #[test]
    fn actual_receive_only_selection_keeps_stop_important_ordinary_order() {
        let channels = Channels::new();
        channels
            .ordinary_sender
            .try_send(OwnedMessage::new(2))
            .ok()
            .unwrap();
        channels.sender.try_send(OwnedMessage::new(1)).ok().unwrap();
        channels.stop_sender.try_send(()).unwrap();
        assert!(matches!(
            select_once(&channels, Duration::ZERO).unwrap(),
            Selected::Stop
        ));
        let Selected::Important(value) = select_once(&channels, Duration::ZERO).unwrap() else {
            panic!("expected important");
        };
        assert!(value.valid(1));
        let Selected::Ordinary(value) = select_once(&channels, Duration::ZERO).unwrap() else {
            panic!("expected ordinary");
        };
        assert!(value.valid(2));
        assert!(matches!(
            select_once(&channels, Duration::ZERO).unwrap(),
            Selected::Timeout
        ));
    }
}

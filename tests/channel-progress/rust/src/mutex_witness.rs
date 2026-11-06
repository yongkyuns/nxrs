//! Single-item std primitive witnesses, not a channel or a three-class owner.
//!
//! A busy one-shot try-lock retains ownership; it is never counted as delivery.
//! Native gates arrange the experiment and teardown, not the transfer or wait.
use crate::{
    join_bounded,
    native::{self, Gate, Result, Schedule, State as NativeState},
    wait_until, Message, Watchdog, HIGH_PRIORITY, LOW_PRIORITY, OBSERVER_PRIORITY, WAIT_MS,
    WORKER_STACK,
};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        Arc, Condvar, Mutex, TryLockError,
    },
    thread,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Case {
    AdmitHeld,
    AdmitReleased,
    TakeHeld,
    TakeReleased,
    WaitPublish,
    PublishBeforeWait,
    CloseWait,
}
impl Case {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "mutex-admit-held" => Some(Self::AdmitHeld),
            "mutex-admit-released" => Some(Self::AdmitReleased),
            "mutex-take-held" => Some(Self::TakeHeld),
            "mutex-take-released" => Some(Self::TakeReleased),
            "condvar-wait-publish" => Some(Self::WaitPublish),
            "condvar-publish-before-wait" => Some(Self::PublishBeforeWait),
            "condvar-close-wait" => Some(Self::CloseWait),
            _ => None,
        }
    }
    fn label(self) -> &'static str {
        match self {
            Self::AdmitHeld => "mutex-admit-held",
            Self::AdmitReleased => "mutex-admit-released",
            Self::TakeHeld => "mutex-take-held",
            Self::TakeReleased => "mutex-take-released",
            Self::WaitPublish => "condvar-wait-publish",
            Self::PublishBeforeWait => "condvar-publish-before-wait",
            Self::CloseWait => "condvar-close-wait",
        }
    }
    fn held(self) -> bool {
        matches!(self, Self::AdmitHeld | Self::TakeHeld)
    }
    fn mutex(self) -> bool {
        matches!(
            self,
            Self::AdmitHeld | Self::AdmitReleased | Self::TakeHeld | Self::TakeReleased
        )
    }
    fn initial_value(self) -> bool {
        matches!(self, Self::TakeHeld | Self::TakeReleased)
    }
    fn waits(self) -> bool {
        matches!(self, Self::WaitPublish | Self::CloseWait)
    }
    fn outcome(self) -> &'static str {
        match self {
            Self::AdmitHeld | Self::TakeHeld => "busy",
            Self::AdmitReleased => "accepted",
            Self::TakeReleased | Self::WaitPublish | Self::PublishBeforeWait => "taken",
            Self::CloseWait => "closed",
        }
    }
}

pub fn recognizes(value: &str) -> bool {
    Case::parse(value).is_some()
}

// Deliberately neither Copy nor Clone. Every success/rejection moves the value.
#[derive(Debug)]
struct OwnedMessage(Message);
impl OwnedMessage {
    fn new() -> Self {
        Self(Message::new(1))
    }
    fn valid(&self) -> bool {
        self.0.valid(1)
    }
}

struct State {
    value: Option<OwnedMessage>,
    closed: bool,
}
struct Primitive {
    state: Mutex<State>,
    changed: Condvar,
}
enum Admission {
    Accepted,
    Busy(OwnedMessage),
    Full(OwnedMessage),
    Closed(OwnedMessage),
}
enum Take {
    Taken(OwnedMessage),
    Busy,
    Empty,
    Closed,
}
impl Primitive {
    fn new(value: Option<OwnedMessage>) -> Self {
        Self {
            state: Mutex::new(State {
                value,
                closed: false,
            }),
            changed: Condvar::new(),
        }
    }
    fn prewarm(&self) -> Result<()> {
        drop(
            self.state
                .lock()
                .map_err(|_| "primitive-prewarm-poisoned")?,
        );
        // std's pthread Condvar backing is initialized on first use as well.
        self.changed.notify_one();
        Ok(())
    }
    fn try_admit(&self, value: OwnedMessage) -> Result<Admission> {
        let mut guard = match self.state.try_lock() {
            Ok(guard) => guard,
            Err(TryLockError::WouldBlock) => return Ok(Admission::Busy(value)),
            Err(TryLockError::Poisoned(_)) => return Err("primitive-admit-poisoned"),
        };
        if guard.closed {
            Ok(Admission::Closed(value))
        } else if guard.value.is_some() {
            Ok(Admission::Full(value))
        } else {
            guard.value = Some(value);
            Ok(Admission::Accepted)
        }
    }
    fn try_take(&self) -> Result<Take> {
        let mut guard = match self.state.try_lock() {
            Ok(guard) => guard,
            Err(TryLockError::WouldBlock) => return Ok(Take::Busy),
            Err(TryLockError::Poisoned(_)) => return Err("primitive-take-poisoned"),
        };
        if guard.closed {
            Ok(Take::Closed)
        } else if let Some(value) = guard.value.take() {
            Ok(Take::Taken(value))
        } else {
            Ok(Take::Empty)
        }
    }
    fn wait_take(&self, mut before_wait: impl FnMut()) -> Result<Take> {
        let mut guard = self.state.lock().map_err(|_| "primitive-wait-poisoned")?;
        loop {
            if guard.closed {
                return Ok(Take::Closed);
            }
            if let Some(value) = guard.value.take() {
                return Ok(Take::Taken(value));
            }
            before_wait();
            // This predicate loop blocks on every unsuccessful check. It is
            // not a try-lock retry, yield loop, or timer-based polling path.
            guard = self
                .changed
                .wait(guard)
                .map_err(|_| "primitive-condvar-poisoned")?;
        }
    }
    fn cancel_wait(&self) -> Result<()> {
        let (mut guard, poisoned) = match self.state.lock() {
            Ok(guard) => (guard, false),
            Err(error) => (error.into_inner(), true),
        };
        guard.closed = true;
        drop(guard);
        self.changed.notify_all();
        if poisoned {
            Err("primitive-cleanup-poisoned")
        } else {
            Ok(())
        }
    }
}

#[derive(Default)]
struct Worker {
    tid: AtomicI32,
    done: AtomicBool,
    failed: AtomicBool,
}
impl Worker {
    fn setup(&self, priority: i32) -> Result<()> {
        let tid = native::tid();
        native::fifo(tid, priority)?;
        self.tid.store(tid, Ordering::Release);
        Ok(())
    }
    fn waiting(&self, priority: i32) -> bool {
        let tid = self.tid.load(Ordering::Acquire);
        tid > 0 && native::state(tid) == (NativeState::WaitSem, priority)
    }
    fn schedule(&self) -> Result<Schedule> {
        native::schedule(self.tid.load(Ordering::Acquire))
    }
}

#[derive(Default)]
struct Evidence {
    next_order: AtomicU32,
    prewarm_order: AtomicU32,
    caller_start_order: AtomicU32,
    holder_start_order: AtomicU32,
    holder_guard_order: AtomicU32,
    caller_attempt_order: AtomicU32,
    caller_return_order: AtomicU32,
    holder_release_order: AtomicU32,
    wait_verified_order: AtomicU32,
    publish_order: AtomicU32,
    close_order: AtomicU32,
    holding: AtomicBool,
    guard_held_at_attempt: AtomicBool,
    guard_held_at_return: AtomicBool,
    try_lock_calls: AtomicU32,
    condvar_wait_calls: AtomicU32,
    busy_count: AtomicU32,
    accepted: AtomicU32,
    transferred: AtomicU32,
    original_payload_retained: AtomicBool,
    protected_payload_retained: AtomicBool,
    cancelled: AtomicBool,
    caller: Worker,
    holder: Worker,
}
impl Evidence {
    fn mark(&self, field: &AtomicU32) {
        field.store(
            self.next_order.fetch_add(1, Ordering::AcqRel) + 1,
            Ordering::Release,
        );
    }
    fn cancelled(&self) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            Err("primitive-case-cancelled")
        } else {
            Ok(())
        }
    }
}

struct Gates {
    ready: Arc<Gate>,
    caller_start: Arc<Gate>,
    holder_start: Arc<Gate>,
    caller_finish: Arc<Gate>,
    holder_finish: Arc<Gate>,
}
impl Gates {
    fn new() -> Result<Self> {
        Ok(Self {
            ready: Gate::new()?,
            caller_start: Gate::new()?,
            holder_start: Gate::new()?,
            caller_finish: Gate::new()?,
            holder_finish: Gate::new()?,
        })
    }
    fn release_all(&self) -> Result<()> {
        // Always attempt every post even when an earlier cleanup action fails.
        let posts = [
            self.caller_start.post(),
            self.holder_start.post(),
            self.caller_finish.post(),
            self.holder_finish.post(),
        ];
        for result in posts {
            result?;
        }
        Ok(())
    }
}

struct CallerOutput {
    retained: Option<OwnedMessage>,
    taken: Option<OwnedMessage>,
}

fn caller_operation(
    case: Case,
    primitive: &Primitive,
    evidence: &Evidence,
) -> Result<CallerOutput> {
    evidence.cancelled()?;
    evidence.mark(&evidence.caller_attempt_order);
    let held = evidence.holding.load(Ordering::Acquire);
    evidence
        .guard_held_at_attempt
        .store(held, Ordering::Release);
    let mut output = CallerOutput {
        retained: None,
        taken: None,
    };
    if case.mutex() {
        evidence.try_lock_calls.fetch_add(1, Ordering::Relaxed);
        if matches!(case, Case::AdmitHeld | Case::AdmitReleased) {
            match primitive.try_admit(OwnedMessage::new())? {
                Admission::Busy(value) if case == Case::AdmitHeld && value.valid() => {
                    evidence.busy_count.store(1, Ordering::Relaxed);
                    evidence
                        .original_payload_retained
                        .store(true, Ordering::Release);
                    output.retained = Some(value);
                }
                Admission::Accepted if case == Case::AdmitReleased => {
                    evidence.accepted.store(1, Ordering::Release);
                }
                Admission::Full(value) | Admission::Closed(value) => {
                    // Rejections still own their value, including failure paths.
                    let _retained = value;
                    return Err("primitive-unexpected-admission-rejection");
                }
                _ => return Err("primitive-unexpected-admission"),
            }
        } else {
            match primitive.try_take()? {
                Take::Busy if case == Case::TakeHeld => {
                    evidence.busy_count.store(1, Ordering::Release);
                }
                Take::Taken(value) if case == Case::TakeReleased && value.valid() => {
                    evidence.transferred.store(1, Ordering::Release);
                    output.taken = Some(value);
                }
                _ => return Err("primitive-unexpected-take"),
            }
        }
    } else {
        match primitive.wait_take(|| {
            evidence.condvar_wait_calls.fetch_add(1, Ordering::Release);
        })? {
            Take::Closed if case == Case::CloseWait => {}
            Take::Taken(value) if case != Case::CloseWait && value.valid() => {
                evidence.transferred.store(1, Ordering::Release);
                output.taken = Some(value);
            }
            _ => return Err("primitive-unexpected-wait-result"),
        }
    }
    evidence
        .guard_held_at_return
        .store(evidence.holding.load(Ordering::Acquire), Ordering::Release);
    evidence.mark(&evidence.caller_return_order);
    Ok(output)
}

fn caller(
    case: Case,
    primitive: &Primitive,
    evidence: &Evidence,
    gates: &Gates,
) -> Result<CallerOutput> {
    let setup = evidence.caller.setup(HIGH_PRIORITY);
    evidence.mark(&evidence.caller_start_order);
    gates.ready.post()?;
    let result = setup
        .and_then(|()| gates.caller_start.wait(WAIT_MS))
        .and_then(|()| caller_operation(case, primitive, evidence));
    evidence
        .caller
        .failed
        .store(result.is_err(), Ordering::Release);
    evidence.caller.done.store(true, Ordering::Release);
    let finish = gates.caller_finish.wait(WAIT_MS);
    let output = result?;
    finish?;
    Ok(output)
}

fn holder_operation(
    case: Case,
    primitive: &Primitive,
    evidence: &Evidence,
    gates: &Gates,
) -> Result<()> {
    evidence.cancelled()?;
    let mut guard = primitive
        .state
        .lock()
        .map_err(|_| "primitive-holder-poisoned")?;
    evidence.holding.store(true, Ordering::Release);
    evidence.mark(&evidence.holder_guard_order);
    if case.held() {
        // Posting preempts this low holder with the high one-shot caller. The
        // holder never waits at a native gate while it owns the std mutex.
        gates.caller_start.post()?;
        if !evidence.caller.done.load(Ordering::Acquire)
            || evidence.caller.failed.load(Ordering::Acquire)
        {
            return Err("primitive-caller-did-not-return-under-guard");
        }
        if case == Case::TakeHeld {
            if !guard.value.as_ref().is_some_and(OwnedMessage::valid) {
                return Err("primitive-protected-value-lost");
            }
            evidence
                .protected_payload_retained
                .store(true, Ordering::Release);
        } else if guard.value.is_some() {
            return Err("primitive-busy-admission-modified-state");
        }
    } else if !case.mutex() {
        if case == Case::CloseWait {
            guard.closed = true;
            evidence.mark(&evidence.close_order);
        } else {
            if guard.closed || guard.value.is_some() {
                return Err("primitive-publish-state");
            }
            guard.value = Some(OwnedMessage::new());
            evidence.accepted.store(1, Ordering::Release);
            evidence.mark(&evidence.publish_order);
        }
    }
    drop(guard);
    evidence.holding.store(false, Ordering::Release);
    evidence.mark(&evidence.holder_release_order);
    if case.mutex() && !case.held() {
        gates.caller_start.post()?;
        if !evidence.caller.done.load(Ordering::Acquire)
            || evidence.caller.failed.load(Ordering::Acquire)
        {
            return Err("primitive-released-caller-did-not-return");
        }
        if case == Case::AdmitReleased {
            let value = primitive
                .state
                .lock()
                .map_err(|_| "primitive-consume-poisoned")?
                .value
                .take();
            if !value.as_ref().is_some_and(OwnedMessage::valid) {
                return Err("primitive-accepted-value-lost");
            }
            evidence.transferred.store(1, Ordering::Release);
        }
    } else if !case.mutex() {
        primitive.changed.notify_all();
    }
    Ok(())
}

fn holder(case: Case, primitive: &Primitive, evidence: &Evidence, gates: &Gates) -> Result<()> {
    let setup = evidence.holder.setup(LOW_PRIORITY);
    evidence.mark(&evidence.holder_start_order);
    gates.ready.post()?;
    let result = setup
        .and_then(|()| gates.holder_start.wait(WAIT_MS))
        .and_then(|()| holder_operation(case, primitive, evidence, gates));
    // An error also leaves the function's RAII guard dropped.
    evidence.holding.store(false, Ordering::Release);
    evidence
        .holder
        .failed
        .store(result.is_err(), Ordering::Release);
    evidence.holder.done.store(true, Ordering::Release);
    let finish = gates.holder_finish.wait(WAIT_MS);
    result?;
    finish
}

struct Observation {
    caller: Schedule,
    holder: Schedule,
    initial_wait_state: NativeState,
    initial_wait_priority: i32,
}
fn observe(case: Case, evidence: &Evidence, gates: &Gates) -> Result<Observation> {
    gates.ready.wait(WAIT_MS)?;
    gates.ready.wait(WAIT_MS)?;
    wait_until(|| evidence.caller.waiting(HIGH_PRIORITY) && evidence.holder.waiting(LOW_PRIORITY))?;
    let caller = evidence.caller.schedule()?;
    let holder = evidence.holder.schedule()?;
    if caller.policy != 1
        || caller.priority != HIGH_PRIORITY
        || holder.policy != 1
        || holder.priority != LOW_PRIORITY
    {
        return Err("primitive-worker-scheduling");
    }
    let (mut initial_wait_state, mut initial_wait_priority) = (NativeState::Unavailable, -1);
    if case.waits() {
        gates.caller_start.post()?;
        wait_until(|| {
            evidence.condvar_wait_calls.load(Ordering::Acquire) > 0
                && !evidence.caller.done.load(Ordering::Acquire)
                && evidence.caller.waiting(HIGH_PRIORITY)
        })?;
        // The start gate has returned, wait_take has entered, and no finish
        // gate is reachable before done. WAIT_SEM therefore observes its real
        // Condvar wait; a readiness announcement alone cannot satisfy this.
        (initial_wait_state, initial_wait_priority) =
            native::state(evidence.caller.tid.load(Ordering::Acquire));
        evidence.mark(&evidence.wait_verified_order);
        gates.holder_start.post()?;
    } else {
        gates.holder_start.post()?;
        if case == Case::PublishBeforeWait {
            wait_until(|| evidence.holder.done.load(Ordering::Acquire))?;
            gates.caller_start.post()?;
        }
    }
    wait_until(|| {
        evidence.caller.done.load(Ordering::Acquire) && evidence.holder.done.load(Ordering::Acquire)
    })?;
    if evidence.caller.failed.load(Ordering::Acquire)
        || evidence.holder.failed.load(Ordering::Acquire)
    {
        return Err("primitive-worker-operation");
    }
    // Both operation bodies have completed naturally before teardown. These
    // final native gates keep their scheduling readback available until joins.
    wait_until(|| evidence.caller.waiting(HIGH_PRIORITY) && evidence.holder.waiting(LOW_PRIORITY))?;
    let caller_after = evidence.caller.schedule()?;
    let holder_after = evidence.holder.schedule()?;
    if caller_after.policy != caller.policy
        || caller_after.priority != caller.priority
        || holder_after.policy != holder.policy
        || holder_after.priority != holder.priority
    {
        return Err("primitive-worker-priority-changed");
    }
    Ok(Observation {
        caller,
        holder,
        initial_wait_state,
        initial_wait_priority,
    })
}

struct Report {
    evidence: Arc<Evidence>,
    observation: Observation,
    observer: Schedule,
    final_value_count: u32,
}

fn target_case(case: Case) -> Result<Report> {
    let observer = native::fifo(native::tid(), OBSERVER_PRIORITY)?;
    let primitive = Arc::new(Primitive::new(case.initial_value().then(OwnedMessage::new)));
    let evidence = Arc::new(Evidence::default());
    let gates = Arc::new(Gates::new()?);
    primitive.prewarm()?;
    evidence.mark(&evidence.prewarm_order);
    let caller_handle = {
        let (primitive, evidence, gates) = (primitive.clone(), evidence.clone(), gates.clone());
        thread::Builder::new()
            .stack_size(WORKER_STACK)
            .spawn(move || caller(case, &primitive, &evidence, &gates))
            .map_err(|_| "primitive-caller-spawn")?
    };
    let holder_handle = {
        let (primitive_worker, evidence_worker, gates_worker) =
            (primitive.clone(), evidence.clone(), gates.clone());
        match thread::Builder::new()
            .stack_size(WORKER_STACK)
            .spawn(move || holder(case, &primitive_worker, &evidence_worker, &gates_worker))
        {
            Ok(handle) => handle,
            Err(_) => {
                evidence.cancelled.store(true, Ordering::Release);
                let released = gates.release_all();
                let closed = primitive.cancel_wait();
                let joined = join_bounded(caller_handle);
                released?;
                closed?;
                // The cancelled worker's error is expected on partial start.
                if let Err(reason) = joined {
                    if reason != "primitive-case-cancelled" {
                        return Err(reason);
                    }
                }
                return Err("primitive-holder-spawn");
            }
        }
    };
    let observation = observe(case, &evidence, &gates);
    let cancelled = if observation.is_err() {
        evidence.cancelled.store(true, Ordering::Release);
        // Release setup/finish gates before taking the predicate lock, so no
        // live peer depends on the observer while cancellation takes that lock.
        let released = gates.release_all();
        let closed = primitive.cancel_wait();
        released.and(closed)
    } else {
        let caller_finish = gates.caller_finish.post();
        let holder_finish = gates.holder_finish.post();
        caller_finish.and(holder_finish)
    };
    let caller_result = join_bounded(caller_handle);
    let holder_result = join_bounded(holder_handle);
    cancelled?;
    let output = caller_result?;
    holder_result?;
    let observation = observation?;
    let guard = primitive
        .state
        .lock()
        .map_err(|_| "primitive-final-state-poisoned")?;
    let final_value_count = u32::from(guard.value.is_some());
    if final_value_count != u32::from(case == Case::TakeHeld)
        || guard.value.as_ref().is_some_and(|value| !value.valid())
        || guard.closed != (case == Case::CloseWait)
        || output.retained.is_some() != (case == Case::AdmitHeld)
        || output.retained.as_ref().is_some_and(|value| !value.valid())
        || output.taken.is_some()
            != matches!(
                case,
                Case::TakeReleased | Case::WaitPublish | Case::PublishBeforeWait
            )
        || output.taken.as_ref().is_some_and(|value| !value.valid())
    {
        return Err("primitive-final-ownership");
    }
    drop(guard);
    Ok(Report {
        evidence,
        observation,
        observer,
        final_value_count,
    })
}

fn read(value: &AtomicU32) -> u32 {
    value.load(Ordering::Acquire)
}
fn flag(value: &AtomicBool) -> bool {
    value.load(Ordering::Acquire)
}

impl Report {
    fn valid(&self, case: Case) -> bool {
        let e = &self.evidence;
        let expected_accepted = u32::from(matches!(
            case,
            Case::AdmitReleased | Case::WaitPublish | Case::PublishBeforeWait
        ));
        let expected_transferred = u32::from(matches!(
            case,
            Case::AdmitReleased | Case::TakeReleased | Case::WaitPublish | Case::PublishBeforeWait
        ));
        let before_workers = read(&e.prewarm_order) > 0
            && read(&e.prewarm_order) < read(&e.caller_start_order)
            && read(&e.prewarm_order) < read(&e.holder_start_order)
            && read(&e.caller_start_order) < read(&e.caller_attempt_order)
            && read(&e.holder_start_order) < read(&e.holder_guard_order);
        let required_orders = [
            read(&e.prewarm_order),
            read(&e.caller_start_order),
            read(&e.holder_start_order),
            read(&e.holder_guard_order),
            read(&e.caller_attempt_order),
            read(&e.caller_return_order),
            read(&e.holder_release_order),
        ];
        let distinct_orders = required_orders
            .iter()
            .enumerate()
            .all(|(index, order)| *order > 0 && !required_orders[..index].contains(order));
        let publication = if matches!(case, Case::WaitPublish | Case::PublishBeforeWait) {
            read(&e.holder_guard_order) < read(&e.publish_order)
                && read(&e.publish_order) < read(&e.holder_release_order)
        } else {
            read(&e.publish_order) == 0
        };
        let closure = if case == Case::CloseWait {
            read(&e.holder_guard_order) < read(&e.close_order)
                && read(&e.close_order) < read(&e.holder_release_order)
        } else {
            read(&e.close_order) == 0
        };
        let wait_proof = if case.waits() {
            self.observation.initial_wait_state == NativeState::WaitSem
                && self.observation.initial_wait_priority == HIGH_PRIORITY
                && read(&e.wait_verified_order) > 0
        } else {
            self.observation.initial_wait_state == NativeState::Unavailable
                && self.observation.initial_wait_priority == -1
                && read(&e.wait_verified_order) == 0
        };
        let ordering = if case.held() {
            read(&e.holder_guard_order) < read(&e.caller_attempt_order)
                && read(&e.caller_attempt_order) < read(&e.caller_return_order)
                && read(&e.caller_return_order) < read(&e.holder_release_order)
        } else if case.mutex() || case == Case::PublishBeforeWait {
            read(&e.holder_guard_order) < read(&e.holder_release_order)
                && read(&e.holder_release_order) < read(&e.caller_attempt_order)
                && read(&e.caller_attempt_order) < read(&e.caller_return_order)
        } else {
            let mutation = if case == Case::CloseWait {
                read(&e.close_order)
            } else {
                read(&e.publish_order)
            };
            read(&e.caller_attempt_order) < read(&e.wait_verified_order)
                && read(&e.wait_verified_order) < read(&e.holder_guard_order)
                && read(&e.holder_guard_order) < mutation
                && mutation < read(&e.holder_release_order)
                && read(&e.holder_release_order) < read(&e.caller_return_order)
        };
        before_workers
            && distinct_orders
            && publication
            && closure
            && wait_proof
            && ordering
            && read(&e.try_lock_calls) == u32::from(case.mutex())
            && read(&e.busy_count) == u32::from(case.held())
            && read(&e.accepted) == expected_accepted
            && read(&e.transferred) == expected_transferred
            && flag(&e.original_payload_retained) == (case == Case::AdmitHeld)
            && flag(&e.protected_payload_retained) == (case == Case::TakeHeld)
            && flag(&e.guard_held_at_attempt) == case.held()
            && flag(&e.guard_held_at_return) == case.held()
            && (read(&e.condvar_wait_calls) > 0) == case.waits()
            && flag(&e.caller.done)
            && flag(&e.holder.done)
            && !flag(&e.cancelled)
    }
    fn print(&self, case: Case, run_id: &str, watchdog: Schedule) -> Result<()> {
        let e = &self.evidence;
        let o = &self.observation;
        let success = self.valid(case);
        println!(concat!("CHANNEL_PROGRESS {{\"schema\":\"std-transfer-progress-v1\",\"record\":\"metadata\",",
            "\"run_id\":\"{}\",\"build_id\":\"{}\",\"build_mode\":\"std-mutex-suite\",\"mode\":\"{}\",\"hook_stage\":0,",
            "\"runtime\":\"nuttx\",\"transport\":\"std-mutex-condvar\",\"capacity\":1,\"payload_bytes\":16,",
            "\"provider_count\":1,\"arm_count\":1,\"selection_bias\":\"none\",\"witness_api\":\"{}\",",
            "\"single_core_tcb_witness\":true,\"prewarmed\":true,\"prewarm_before_workers\":true,",
            "\"caller_policy\":{},\"caller_priority\":{},\"holder_policy\":{},\"holder_priority\":{},",
            "\"observer_policy\":{},\"observer_priority\":{},\"watchdog_policy\":{},\"watchdog_priority\":{}}}"),
            run_id, env!("NXRS_CHANNEL_PROGRESS_BUILD_ID"), case.label(), if case.mutex() { "try_lock" } else { "condvar" },
            o.caller.policy, o.caller.priority, o.holder.policy, o.holder.priority,
            self.observer.policy, self.observer.priority, watchdog.policy, watchdog.priority);
        println!(concat!("CHANNEL_PROGRESS {{\"schema\":\"std-transfer-progress-v1\",\"record\":\"result\",",
            "\"run_id\":\"{}\",\"mode\":\"{}\",\"outcome\":\"{}\",\"success\":{},\"diagnostic_valid\":{},",
            "\"completed_without_repair\":{},\"primitive_contract_satisfied\":{},\"priority_progress_qualified\":false,",
            "\"production_transport_qualified\":false,\"timing_qualified\":false,\"recovery_applied\":false,",
            "\"try_lock_calls\":{},\"busy_count\":{},\"accepted\":{},\"transferred\":{},\"initial_value_count\":{},\"final_value_count\":{},",
            "\"original_payload_retained\":{},\"protected_payload_retained\":{},\"payload_valid\":true,",
            "\"guard_held_at_attempt\":{},\"guard_held_at_return\":{},\"condvar_wait_calls\":{},\"initial_condvar_wait_verified\":{},",
            "\"initial_wait_state\":\"{}\",\"initial_wait_priority\":{},\"caller_start_gate_verified\":true,\"holder_start_gate_verified\":true,",
            "\"worker_priorities_verified\":true,\"caller_done\":{},\"holder_done\":{},\"joins_complete\":true,\"watchdog_complete\":true,",
            "\"prewarm_order\":{},\"caller_start_order\":{},\"holder_start_order\":{},\"holder_guard_order\":{},",
            "\"caller_attempt_order\":{},\"caller_return_order\":{},\"holder_release_order\":{},\"wait_verified_order\":{},\"publish_order\":{},\"close_order\":{}}}"),
            run_id, case.label(), case.outcome(), success, success, success, success,
            read(&e.try_lock_calls), read(&e.busy_count), read(&e.accepted), read(&e.transferred), u32::from(case.initial_value()), self.final_value_count,
            flag(&e.original_payload_retained), flag(&e.protected_payload_retained), flag(&e.guard_held_at_attempt), flag(&e.guard_held_at_return),
            read(&e.condvar_wait_calls), case.waits(), o.initial_wait_state.label(), o.initial_wait_priority,
            flag(&e.caller.done), flag(&e.holder.done), read(&e.prewarm_order), read(&e.caller_start_order), read(&e.holder_start_order),
            read(&e.holder_guard_order), read(&e.caller_attempt_order), read(&e.caller_return_order), read(&e.holder_release_order),
            read(&e.wait_verified_order), read(&e.publish_order), read(&e.close_order));
        if success {
            Ok(())
        } else {
            Err("primitive-invalid-evidence")
        }
    }
}

pub fn run(value: &str) -> Result<()> {
    let case = Case::parse(value).ok_or("unknown-std-transfer-case")?;
    if !native::is_nuttx() {
        return Err("std-transfer-witness-requires-single-core-flat-nuttx");
    }
    if env!("NXRS_CHANNEL_PROGRESS_HOOK_STAGE") != "0" {
        return Err("std-transfer-requires-clean-crossbeam-build");
    }
    let run_id = format!("{}-{}", native::tid(), native::monotonic_ns()?);
    let original = native::schedule(native::tid())?;
    let watchdog = Watchdog::start()?;
    let watchdog_schedule = watchdog.schedule;
    let result = target_case(case);
    // The watchdog stays live through every worker join. Restoring only the
    // observer's own original policy is teardown, never worker priority repair.
    let restore = native::set_schedule(native::tid(), original);
    let stopped = watchdog.finish();
    restore?;
    stopped?;
    result?.print(case, &run_id, watchdog_schedule)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::mpsc, time::Duration};

    #[test]
    fn busy_admission_retains_owned_input_and_never_fills_slot() {
        let primitive = Primitive::new(None);
        primitive.prewarm().unwrap();
        let guard = primitive.state.lock().unwrap();
        let Admission::Busy(value) = primitive.try_admit(OwnedMessage::new()).unwrap() else {
            panic!("expected busy")
        };
        assert!(value.valid());
        assert!(guard.value.is_none());
        assert_eq!(std::mem::size_of::<OwnedMessage>(), 16);
    }

    #[test]
    fn busy_take_preserves_the_protected_owned_value() {
        let primitive = Primitive::new(Some(OwnedMessage::new()));
        let guard = primitive.state.lock().unwrap();
        assert!(matches!(primitive.try_take().unwrap(), Take::Busy));
        assert!(guard.value.as_ref().unwrap().valid());
        drop(guard);
        let Take::Taken(value) = primitive.try_take().unwrap() else {
            panic!("expected owned take")
        };
        assert!(value.valid());
        assert!(matches!(primitive.try_take().unwrap(), Take::Empty));
    }

    #[test]
    fn admission_full_and_closed_return_the_original_value() {
        let primitive = Primitive::new(None);
        assert!(matches!(
            primitive.try_admit(OwnedMessage::new()).unwrap(),
            Admission::Accepted
        ));
        let Admission::Full(value) = primitive.try_admit(OwnedMessage::new()).unwrap() else {
            panic!("expected full")
        };
        assert!(value.valid());
        primitive.cancel_wait().unwrap();
        let Admission::Closed(value) = primitive.try_admit(value).unwrap() else {
            panic!("expected closed")
        };
        assert!(value.valid());
        assert!(matches!(primitive.try_take().unwrap(), Take::Closed));
    }

    #[test]
    fn publication_before_wait_needs_no_notification_token() {
        let primitive = Primitive::new(Some(OwnedMessage::new()));
        primitive.prewarm().unwrap();
        let mut waits = 0;
        let Take::Taken(value) = primitive.wait_take(|| waits += 1).unwrap() else {
            panic!("expected predicate value")
        };
        assert!(value.valid());
        assert_eq!(waits, 0);
    }

    fn pending_wait(close: bool) {
        let primitive = Arc::new(Primitive::new(None));
        primitive.prewarm().unwrap();
        let (entered_tx, entered_rx) = mpsc::sync_channel(0);
        let (done_tx, done_rx) = mpsc::channel();
        let worker_primitive = primitive.clone();
        let worker = thread::spawn(move || {
            let mut first = true;
            let result = worker_primitive.wait_take(|| {
                if first {
                    first = false;
                    entered_tx.send(()).unwrap();
                }
            });
            done_tx.send(result).unwrap();
        });
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        // The announcement occurs while holding the actual predicate mutex.
        // Acquiring it forces wait's unlock/registration boundary; publication
        // here cannot slip through a check-before-wait gap.
        let mut guard = primitive.state.lock().unwrap();
        if close {
            guard.closed = true;
        } else {
            guard.value = Some(OwnedMessage::new());
        }
        drop(guard);
        primitive.changed.notify_all();
        let completed = done_rx.recv_timeout(Duration::from_secs(2));
        if completed.is_err() {
            primitive.cancel_wait().unwrap();
        }
        worker.join().unwrap();
        match completed.unwrap().unwrap() {
            Take::Closed if close => {
                assert!(matches!(
                    primitive
                        .wait_take(|| panic!("closed predicate must not wait"))
                        .unwrap(),
                    Take::Closed
                ));
            }
            Take::Taken(value) if !close => assert!(value.valid()),
            _ => panic!("unexpected wait outcome"),
        }
    }

    #[test]
    fn publication_at_wait_registration_cannot_be_lost() {
        pending_wait(false);
    }

    #[test]
    fn protected_close_wakes_wait_and_remains_sticky() {
        pending_wait(true);
    }

    #[test]
    fn host_refuses_all_native_progress_modes() {
        if native::is_nuttx() {
            return;
        }
        for case in [
            Case::AdmitHeld,
            Case::AdmitReleased,
            Case::TakeHeld,
            Case::TakeReleased,
            Case::WaitPublish,
            Case::PublishBeforeWait,
            Case::CloseWait,
        ] {
            assert!(recognizes(case.label()));
            assert_eq!(
                run(case.label()),
                Err("std-transfer-witness-requires-single-core-flat-nuttx")
            );
        }
        assert!(!recognizes("host-functional"));
    }
}

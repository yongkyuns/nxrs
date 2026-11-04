//! Deterministic preemption at the real private-std publication boundary.
//! The hook's separate semaphore wakes the consumer. No sleep substitutes for
//! reserving a real array-channel slot or publishing its real slot stamp.
use crate::{
    native::{self, Gate, Result, Schedule, State},
    Message, CAPACITY, OBSERVER_PRIORITY, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS,
    WORKER_STACK,
};
use std::{
    ffi::c_void,
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicPtr, AtomicU32, Ordering},
        mpsc::sync_channel,
        Arc,
    },
    thread,
    time::Duration,
};

static HOOK_STAGE: AtomicU32 = AtomicU32::new(0);
static HOOK_PRODUCER: AtomicI32 = AtomicI32::new(-1);
static HOOK_GATE: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());
static HOOK_HITS: AtomicU32 = AtomicU32::new(0);
static HOOK_RETURNED: AtomicBool = AtomicBool::new(false);
static HOOK_ERROR: AtomicBool = AtomicBool::new(false);
static HOOK_NS_LOW: AtomicU32 = AtomicU32::new(0);
static HOOK_NS_HIGH: AtomicU32 = AtomicU32::new(0);

// ABI shared only with the source-hash-checked private std build. No production
// std source or channel implementation is replaced by this fixture.
#[no_mangle]
pub extern "C" fn nxrs_std_channel_test_hook(stage: u32) {
    if stage == 0
        || stage != HOOK_STAGE.load(Ordering::Acquire)
        || native::tid() != HOOK_PRODUCER.load(Ordering::Relaxed)
    {
        return;
    }
    if HOOK_HITS.load(Ordering::Relaxed) != 0 {
        HOOK_ERROR.store(true, Ordering::Release);
        return;
    }
    match native::monotonic_ns() {
        Ok(stamp) => {
            HOOK_NS_LOW.store(stamp as u32, Ordering::Relaxed);
            HOOK_NS_HIGH.store((stamp >> 32) as u32, Ordering::Relaxed);
        }
        Err(_) => {
            HOOK_ERROR.store(true, Ordering::Release);
        }
    }
    HOOK_HITS.store(1, Ordering::Release);
    let gate = HOOK_GATE.load(Ordering::Acquire);
    // Only this hook posts the consumer gate before the observer snapshot.
    // Its higher priority preempts this producer inside sem_post when valid.
    if gate.is_null() || unsafe { native::scp_gate_post(gate) } != 0 {
        HOOK_ERROR.store(true, Ordering::Release);
    }
    HOOK_RETURNED.store(true, Ordering::Release);
}

struct Shared {
    sender_tid: AtomicI32,
    sender_policy: AtomicI32,
    sender_priority: AtomicI32,
    receiver_tid: AtomicI32,
    receiver_policy: AtomicI32,
    receiver_priority: AtomicI32,
    consumer_entered: AtomicBool,
    entered_ns_low: AtomicU32,
    entered_ns_high: AtomicU32,
    consumer_done: AtomicBool,
    producer_done: AtomicBool,
    valid: AtomicBool,
    cancelled: AtomicBool,
}

pub struct Report {
    pub sender: Schedule,
    pub receiver: Schedule,
    pub observer: Schedule,
    stage: u32,
    hook_hits: u32,
    hook_returned_before_recovery: bool,
    consumer_entered: bool,
    consumer_done_before_recovery: bool,
    producer_done_before_recovery: bool,
    consumer_state: State,
    producer_state: State,
    consumer_priority_at_deadline: i32,
    producer_priority_at_deadline: i32,
    hook_timestamp_ns: u64,
    consumer_enter_timestamp_ns: u64,
    observer_timestamp_ns: u64,
    observation_ns: u64,
    recovery_applied: bool,
    delivered: bool,
    success: bool,
}

fn arm_hook(stage: u32, tid: i32, gate: &Gate) {
    HOOK_STAGE.store(0, Ordering::Release);
    HOOK_HITS.store(0, Ordering::Relaxed);
    HOOK_RETURNED.store(false, Ordering::Relaxed);
    HOOK_ERROR.store(false, Ordering::Relaxed);
    HOOK_NS_LOW.store(0, Ordering::Relaxed);
    HOOK_NS_HIGH.store(0, Ordering::Relaxed);
    HOOK_PRODUCER.store(tid, Ordering::Relaxed);
    HOOK_GATE.store(gate.pointer(), Ordering::Release);
    HOOK_STAGE.store(stage, Ordering::Release);
}

fn recover(shared: &Shared) -> Result<bool> {
    let mut changed = false;
    // The observer stays above both workers while changing priorities. This
    // never assumes sched_yield donates execution to a lower-priority thread.
    if !shared.producer_done.load(Ordering::Acquire) {
        let tid = shared.sender_tid.load(Ordering::Acquire);
        if tid > 0 && native::state(tid).0 != State::Absent {
            native::fifo(tid, 200)?;
            changed = true;
        }
    }
    if !shared.consumer_done.load(Ordering::Acquire) {
        let tid = shared.receiver_tid.load(Ordering::Acquire);
        if tid > 0 && native::state(tid).0 != State::Absent {
            native::fifo(tid, 100)?;
            changed = true;
        }
    }
    Ok(changed)
}

pub fn run(stage: u32) -> Result<Report> {
    if !native::is_nuttx() {
        return Err("no-independent-tcb-witness");
    }
    let observer = native::fifo(native::tid(), OBSERVER_PRIORITY)?;
    let ready = Gate::new()?;
    let producer_start = Gate::new()?;
    let consumer_start = Gate::new()?;
    let shared = Arc::new(Shared {
        sender_tid: AtomicI32::new(-1),
        sender_policy: AtomicI32::new(-1),
        sender_priority: AtomicI32::new(-1),
        receiver_tid: AtomicI32::new(-1),
        receiver_policy: AtomicI32::new(-1),
        receiver_priority: AtomicI32::new(-1),
        consumer_entered: AtomicBool::new(false),
        entered_ns_low: AtomicU32::new(0),
        entered_ns_high: AtomicU32::new(0),
        consumer_done: AtomicBool::new(false),
        producer_done: AtomicBool::new(false),
        valid: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
    });
    let (tx, rx) = sync_channel::<Message>(CAPACITY);
    let (worker, ready_worker, start_worker) =
        (shared.clone(), ready.clone(), consumer_start.clone());
    let consumer = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let tid = native::tid();
            let actual = native::fifo(tid, RECEIVER_PRIORITY);
            if let Ok(value) = actual {
                worker
                    .receiver_policy
                    .store(value.policy, Ordering::Relaxed);
                worker
                    .receiver_priority
                    .store(value.priority, Ordering::Relaxed);
                worker.receiver_tid.store(tid, Ordering::Release);
            }
            ready_worker.post()?;
            actual?;
            start_worker.wait(WAIT_MS)?;
            if worker.cancelled.load(Ordering::Acquire) {
                return Ok(());
            }
            let entered = native::monotonic_ns()?;
            worker
                .entered_ns_low
                .store(entered as u32, Ordering::Relaxed);
            worker
                .entered_ns_high
                .store((entered >> 32) as u32, Ordering::Relaxed);
            worker.consumer_entered.store(true, Ordering::Release);
            let message = rx.try_recv().map_err(|_| "witness-try-receive-failed")?;
            worker.valid.store(message.valid(1), Ordering::Relaxed);
            worker.consumer_done.store(true, Ordering::Release);
            Ok(())
        })
        .map_err(|_| "consumer-spawn")?;

    let (worker, ready_worker, start_worker) =
        (shared.clone(), ready.clone(), producer_start.clone());
    let producer = match thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let tid = native::tid();
            let actual = native::fifo(tid, SENDER_PRIORITY);
            if let Ok(value) = actual {
                worker.sender_policy.store(value.policy, Ordering::Relaxed);
                worker
                    .sender_priority
                    .store(value.priority, Ordering::Relaxed);
                worker.sender_tid.store(tid, Ordering::Release);
            }
            ready_worker.post()?;
            actual?;
            start_worker.wait(WAIT_MS)?;
            if worker.cancelled.load(Ordering::Acquire) {
                return Ok(());
            }
            tx.send(Message::new(1))
                .map_err(|_| "witness-send-disconnected")?;
            worker.producer_done.store(true, Ordering::Release);
            Ok(())
        }) {
        Ok(handle) => handle,
        Err(_) => {
            shared.cancelled.store(true, Ordering::Release);
            if consumer_start.post().is_err() {
                crate::fatal("witness-error-release-failed");
            }
            crate::join_bounded(consumer)?;
            return Err("producer-spawn");
        }
    };

    let observed = (|| {
        ready.wait(WAIT_MS)?;
        ready.wait(WAIT_MS)?;
        let sender_tid = shared.sender_tid.load(Ordering::Acquire);
        let receiver_tid = shared.receiver_tid.load(Ordering::Acquire);
        if sender_tid < 1 || receiver_tid < 1 {
            return Err("witness-worker-scheduling");
        }
        let sender = native::schedule(sender_tid)?;
        let receiver = native::schedule(receiver_tid)?;
        if sender.policy != 1
            || receiver.policy != 1
            || sender.priority != SENDER_PRIORITY
            || receiver.priority != RECEIVER_PRIORITY
        {
            return Err("witness-priority-contract");
        }
        // Independently prove both initial native gate waits. The consumer's
        // channel has no message and will be entered only after the real hook.
        crate::wait_until(|| {
            native::state(sender_tid) == (State::WaitSem, SENDER_PRIORITY)
                && native::state(receiver_tid) == (State::WaitSem, RECEIVER_PRIORITY)
        })?;
        arm_hook(stage, sender_tid, &consumer_start);
        producer_start.post()?;
        // Highest test observer sleeps until its explicit observation deadline.
        // The consumer is released by the hook, never by this elapsed duration.
        thread::sleep(Duration::from_millis(100));
        let observer_timestamp_ns = native::monotonic_ns()?;
        let hook_hits = HOOK_HITS.load(Ordering::Acquire);
        let consumer_entered = shared.consumer_entered.load(Ordering::Acquire);
        let consumer_enter_timestamp_ns =
            (u64::from(shared.entered_ns_high.load(Ordering::Relaxed)) << 32)
                | u64::from(shared.entered_ns_low.load(Ordering::Relaxed));
        let hook_timestamp_ns = (u64::from(HOOK_NS_HIGH.load(Ordering::Relaxed)) << 32)
            | u64::from(HOOK_NS_LOW.load(Ordering::Relaxed));
        let observation_ns = observer_timestamp_ns.saturating_sub(consumer_enter_timestamp_ns);
        let consumer_done_before_recovery = shared.consumer_done.load(Ordering::Acquire);
        let producer_done_before_recovery = shared.producer_done.load(Ordering::Acquire);
        let hook_returned_before_recovery = HOOK_RETURNED.load(Ordering::Acquire);
        let (consumer_state, consumer_priority_at_deadline) = native::state(receiver_tid);
        let (producer_state, producer_priority_at_deadline) = native::state(sender_tid);
        let common = hook_hits == 1
            && consumer_entered
            && !HOOK_ERROR.load(Ordering::Acquire)
            && hook_timestamp_ns <= consumer_enter_timestamp_ns
            && consumer_enter_timestamp_ns <= observer_timestamp_ns
            && observation_ns >= 50_000_000;
        let expected = if stage == 1 {
            !consumer_done_before_recovery
                && !producer_done_before_recovery
                && !hook_returned_before_recovery
                && consumer_state == State::Ready
                && producer_state == State::Ready
                && consumer_priority_at_deadline == RECEIVER_PRIORITY
                && producer_priority_at_deadline == SENDER_PRIORITY
        } else {
            consumer_done_before_recovery
                && producer_done_before_recovery
                && hook_returned_before_recovery
        };
        Ok(Report {
            sender,
            receiver,
            observer,
            stage,
            hook_hits,
            hook_returned_before_recovery,
            consumer_entered,
            consumer_done_before_recovery,
            producer_done_before_recovery,
            consumer_state,
            producer_state,
            consumer_priority_at_deadline,
            producer_priority_at_deadline,
            hook_timestamp_ns,
            consumer_enter_timestamp_ns,
            observer_timestamp_ns,
            observation_ns,
            recovery_applied: false,
            delivered: false,
            success: common && expected,
        })
    })();

    // Recovery runs even after a failed observation. Quiesce both workers before
    // clearing the hook pointer or destroying its native semaphore storage.
    let recovered = recover(&shared);
    if recovered.is_err() {
        crate::fatal("priority-recovery-failed");
    }
    if observed.is_err() {
        shared.cancelled.store(true, Ordering::Release);
        if producer_start.post().is_err() || consumer_start.post().is_err() {
            crate::fatal("witness-error-release-failed");
        }
    }
    let joined_producer = crate::join_bounded(producer);
    let joined_consumer = crate::join_bounded(consumer);
    HOOK_STAGE.store(0, Ordering::Release);
    HOOK_GATE.store(std::ptr::null_mut(), Ordering::Release);
    let mut report = observed?;
    joined_producer?;
    joined_consumer?;
    report.recovery_applied = recovered?;
    report.delivered = shared.valid.load(Ordering::Acquire)
        && shared.consumer_done.load(Ordering::Acquire)
        && shared.producer_done.load(Ordering::Acquire);
    report.success &= report.delivered && !HOOK_ERROR.load(Ordering::Acquire);
    Ok(report)
}

impl Report {
    pub fn print(self, run_id: &str) -> Result<()> {
        println!(concat!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"result\",",
            "\"run_id\":\"{}\",\"success\":{},\"threads_joined\":true,\"deadline_hit\":false,",
            "\"hook_hit_count\":{},\"hook_returned_before_recovery\":{},\"consumer_entered\":{},",
            "\"consumer_done_before_recovery\":{},\"producer_done_before_recovery\":{},",
            "\"consumer_state_at_deadline\":\"{}\",\"producer_state_at_deadline\":\"{}\",",
            "\"consumer_priority_at_deadline\":{},\"producer_priority_at_deadline\":{},",
            "\"hook_timestamp_ns\":{},\"consumer_enter_timestamp_ns\":{},\"observer_timestamp_ns\":{},",
            "\"observation_ns\":{},\"observation_deadline_reached\":true,\"initial_gate_waits_verified\":true,",
            "\"consumer_woken_from_hook\":{},\"recovery_applied\":{},\"delivered\":{},",
            "\"reservation_stall_observed\":{},\"priority_progress_qualified\":false,\"timing_qualified\":false}}"),
            run_id, self.success, self.hook_hits, self.hook_returned_before_recovery, self.consumer_entered,
            self.consumer_done_before_recovery, self.producer_done_before_recovery,
            self.consumer_state.label(), self.producer_state.label(), self.consumer_priority_at_deadline, self.producer_priority_at_deadline,
            self.hook_timestamp_ns, self.consumer_enter_timestamp_ns, self.observer_timestamp_ns,
            self.observation_ns, self.hook_hits == 1 && self.consumer_entered, self.recovery_applied,
            self.delivered, self.stage == 1 && self.success);
        if self.success {
            Ok(())
        } else {
            Err("witness-contract-not-observed")
        }
    }
}

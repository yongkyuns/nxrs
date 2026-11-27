//! Fixed-priority witness for the fixture-only pooled capture transport.
//!
//! The low producer pauses inside a claimed physical slot before writing the
//! payload. Another source can use the remaining shared capacity, and the owner
//! observes the outstanding obligation without spinning or restarting early.
use super::{join, observe_wait, spawn, until};
use crate::capture_pool::{self as pool, Key, Owner, Publisher, Record, Rejection, Stage, Status};
use crate::{platform, Outcome};
use serde::Serialize;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering::SeqCst};
use std::sync::{mpsc, Arc, OnceLock};
use std::thread::{self, Thread};
use std::time::{Duration, Instant};

const DEADLINE: Duration = Duration::from_secs(4);
const OTHER_RECORDS: usize = 511;
const RESTART_RECORDS: usize = 2;

#[derive(Clone, Copy, Debug, Serialize)]
struct Checkpoint {
    occupied: u32,
    busy_sources: u32,
    free_slots: usize,
}

impl Checkpoint {
    fn read(owner: &Owner) -> Self {
        let (occupied, busy_sources) = owner.obligations();
        Self {
            occupied,
            busy_sources,
            free_slots: owner.free_slots(),
        }
    }

    fn check(self, occupied: u32, busy_sources: u32, free_slots: usize) {
        assert_eq!(
            (self.occupied, self.busy_sources, self.free_slots),
            (occupied, busy_sources, free_slots)
        );
    }
}

#[derive(Serialize)]
struct Storage {
    payload_heap_bytes: usize,
    index_heap_bytes: usize,
    bitmap_bytes: usize,
    cursor_bytes: usize,
    storage_inline_bytes: usize,
    raw_components_bytes: usize,
    scope: &'static str,
}

#[derive(Serialize)]
pub struct Report {
    scope: &'static str,
    pause_stage: &'static str,
    capacity: usize,
    sources: usize,
    other_source_records: usize,
    restart_records: usize,
    rejected_full: usize,
    rejected_stale: usize,
    source_order: &'static str,
    complete_payloads: bool,
    sticky_terminal: bool,
    clean_restart: bool,
    pause_hits: usize,
    after_claim: Checkpoint,
    full: Checkpoint,
    pending: Checkpoint,
    retired: Checkpoint,
    restarted: Checkpoint,
    first_missing_source: u8,
    first_missing_sequence: u32,
    atomic_16: bool,
    atomic_32: bool,
    pointer_bytes: usize,
    storage: Storage,
    native_configuration: Option<platform::MutexConfiguration>,
    low_after_claim: Option<platform::ThreadState>,
    low_after_other_progress: Option<platform::ThreadState>,
    owner_waiting: Option<platform::ThreadState>,
}

struct Progress {
    after_claim: Checkpoint,
    full: Checkpoint,
    pending: Checkpoint,
    retired: Checkpoint,
    restarted: Checkpoint,
}

#[derive(Default)]
struct Pause {
    thread: OnceLock<Thread>,
    tid: AtomicI32,
    ready: AtomicBool,
    release: AtomicBool,
    hits: AtomicUsize,
}

impl Pause {
    fn prepare(&self, tid: i32) {
        let current = thread::current();
        current.unpark();
        thread::park();
        self.thread.set(current).unwrap();
        self.tid.store(tid, SeqCst);
    }

    fn wait(&self) {
        assert_eq!(self.hits.fetch_add(1, SeqCst), 0);
        let deadline = Instant::now() + DEADLINE;
        self.ready.store(true, SeqCst);
        while !self.release.load(SeqCst) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            assert!(!remaining.is_zero(), "capture producer pause deadline");
            thread::park_timeout(remaining);
        }
    }

    fn resume(&self) {
        self.release.store(true, SeqCst);
        self.thread.get().unwrap().unpark();
    }
}

fn record(source: u8, sequence: u32, session: u32) -> Record {
    Record {
        time_ms: 0x1234_abcd_0000_0000 | (u64::from(session) << 24) | u64::from(sequence),
        sequence,
        values: [
            0x7fc0_0046 ^ sequence,
            !sequence,
            0x8000_0000 | u32::from(source),
            session,
            sequence.rotate_left(13) ^ 0x4460_1234,
            0x3f80_0000 | (u32::from(source) << 8),
        ],
    }
}

fn waiting_state(tid: i32, priority: i32) -> Option<platform::ThreadState> {
    let state = platform::thread_state(tid);
    if let Some(state) = &state {
        assert_eq!(state.tid, tid);
        assert_eq!(Some(state.policy), platform::fifo_policy());
        assert_eq!(state.base_priority, priority);
        assert_eq!(state.effective_priority, priority);
        assert!(state.waiting_semaphore);
        assert!(!state.runnable);
    }
    state
}

fn progress(
    mut owner: Owner,
    mut publisher: Publisher,
    epoch: u32,
    pending: &AtomicBool,
) -> (Owner, Publisher, Progress) {
    let after_claim = Checkpoint::read(&owner);
    after_claim.check(1, 1, 511);
    assert!(owner.poll().is_none());

    for sequence in 0..OTHER_RECORDS as u32 {
        publisher.offer(epoch, record(1, sequence, 1)).unwrap();
    }
    let missing = record(1, OTHER_RECORDS as u32, 1);
    let rejected = publisher.offer(epoch, missing).unwrap_err();
    assert_eq!(rejected.reason, Rejection::Full);
    assert_eq!(rejected.record, missing);
    let full = Checkpoint::read(&owner);
    full.check(512, 1, 0);

    for sequence in 0..OTHER_RECORDS as u32 {
        let captured = owner.poll().expect("committed other-source record");
        assert_eq!(captured.source, 1);
        assert_eq!(captured.record, record(1, sequence, 1));
    }
    assert!(owner.poll().is_none());
    let pending_checkpoint = Checkpoint::read(&owner);
    pending_checkpoint.check(1, 1, 511);
    assert_eq!(owner.status(), Status::Pending);
    assert!(owner.finish().is_err());
    assert!(owner.begin().is_err());

    let deadline = Instant::now() + DEADLINE;
    let mut final_record = false;
    pending.store(true, SeqCst);
    loop {
        owner.take_ready();
        if let Some(captured) = owner.poll() {
            assert!(!final_record);
            assert_eq!(captured.source, 0);
            assert_eq!(captured.record, record(0, 0, 1));
            final_record = true;
        }
        match owner.status() {
            Status::Overflow(Key {
                source: 1,
                sequence: 511,
            }) => {
                assert!(final_record);
                break;
            }
            Status::Pending => {}
            state => panic!("unexpected capture state: {state:?}"),
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(!remaining.is_zero(), "capture owner progress deadline");
        if !owner.take_ready() {
            thread::park_timeout(remaining);
        }
    }

    let terminal = Status::Overflow(Key {
        source: 1,
        sequence: OTHER_RECORDS as u32,
    });
    assert_eq!(owner.status(), terminal);
    assert!(owner.begin().is_err());
    let retired = Checkpoint::read(&owner);
    retired.check(0, 0, 512);
    assert_eq!(owner.finish().unwrap(), terminal);
    assert_eq!(owner.status(), Status::Inactive);

    let next_epoch = owner.begin().unwrap();
    assert_ne!(next_epoch, epoch);
    let stale = record(1, 0, 1);
    let rejected = publisher.offer(epoch, stale).unwrap_err();
    assert_eq!(rejected.reason, Rejection::Closed);
    assert_eq!(rejected.record, stale);
    for sequence in 0..RESTART_RECORDS as u32 {
        let fresh = record(1, sequence, 2);
        publisher.offer(next_epoch, fresh).unwrap();
        let captured = owner.poll().unwrap();
        assert_eq!(captured.source, 1);
        assert_eq!(captured.record, fresh);
        assert!(owner.poll().is_none());
    }
    owner.cancel();
    assert_eq!(owner.status(), Status::Cancelled);
    assert_eq!(owner.finish().unwrap(), Status::Cancelled);
    let restarted = Checkpoint::read(&owner);
    restarted.check(0, 0, 512);

    (
        owner,
        publisher,
        Progress {
            after_claim,
            full,
            pending: pending_checkpoint,
            retired,
            restarted,
        },
    )
}

pub fn run() -> Outcome {
    assert!(cfg!(target_has_atomic = "16"));
    assert!(cfg!(target_has_atomic = "32"));
    assert_eq!(pool::CAPACITY, 512);

    let mut outcome = Outcome::default();
    let high_thread = Arc::new(OnceLock::<Thread>::new());
    let high_tid = Arc::new(AtomicI32::new(0));
    let high_bootstrapped = Arc::new(AtomicBool::new(false));
    let run_high = Arc::new(AtomicBool::new(false));
    let pending = Arc::new(AtomicBool::new(false));
    let high_done = Arc::new(AtomicBool::new(false));
    let low_done = Arc::new(AtomicBool::new(false));
    let pause = Arc::new(Pause::default());

    // Bootstrap only. This channel is disconnected before the transport
    // experiment begins; no capture payload passes through it.
    let (bootstrap_tx, bootstrap_rx) = mpsc::sync_channel::<(Owner, Publisher, u32)>(1);
    let high = {
        let high_thread = Arc::clone(&high_thread);
        let high_tid = Arc::clone(&high_tid);
        let high_bootstrapped = Arc::clone(&high_bootstrapped);
        let run_high = Arc::clone(&run_high);
        let pending = Arc::clone(&pending);
        let high_done = Arc::clone(&high_done);
        spawn("capture-owner-high", 180, move |tid| {
            let current = thread::current();
            current.unpark();
            thread::park();
            high_thread.set(current).unwrap();
            high_tid.store(tid, SeqCst);
            let (owner, publisher, epoch) = bootstrap_rx.recv().unwrap();
            drop(bootstrap_rx);
            high_bootstrapped.store(true, SeqCst);
            let deadline = Instant::now() + DEADLINE;
            while !run_high.load(SeqCst) {
                let remaining = deadline.saturating_duration_since(Instant::now());
                assert!(!remaining.is_zero(), "capture bootstrap deadline");
                thread::park_timeout(remaining);
            }
            let result = progress(owner, publisher, epoch, &pending);
            high_done.store(true, SeqCst);
            result
        })
    };
    until(|| high_thread.get().is_some());
    let (mut owner, [mut low_publisher, high_publisher, spare_publisher]) =
        pool::channel(high_thread.get().unwrap().clone());
    let epoch = owner.begin().unwrap();
    bootstrap_tx.send((owner, high_publisher, epoch)).unwrap();
    drop(bootstrap_tx);
    until(|| high_bootstrapped.load(SeqCst));

    let low = {
        let pause = Arc::clone(&pause);
        let low_done = Arc::clone(&low_done);
        spawn("capture-producer-low", 120, move |tid| {
            pause.prepare(tid);
            low_publisher
                .offer_with_hooks(epoch, record(0, 0, 1), |stage| {
                    if stage == Stage::AfterSlotClaim {
                        pause.wait();
                    }
                })
                .unwrap();
            low_done.store(true, SeqCst);
            low_publisher
        })
    };
    observe_wait(&pause.tid, 120, || pause.ready.load(SeqCst), &mut outcome);
    let low_after_claim = waiting_state(pause.tid.load(SeqCst), 120);

    run_high.store(true, SeqCst);
    high_thread.get().unwrap().unpark();
    observe_wait(&high_tid, 180, || pending.load(SeqCst), &mut outcome);
    assert!(!pause.release.load(SeqCst));
    assert!(!high_done.load(SeqCst));
    assert!(!low_done.load(SeqCst));
    let low_after_other_progress = waiting_state(pause.tid.load(SeqCst), 120);
    let owner_waiting = waiting_state(high_tid.load(SeqCst), 180);

    pause.resume();
    until(|| high_done.load(SeqCst) && low_done.load(SeqCst));
    let low_publisher = join(low, &mut outcome);
    let (owner, high_publisher, progress) = join(high, &mut outcome);
    assert_eq!(owner.obligations(), (0, 0));
    assert_eq!(owner.free_slots(), 512);
    drop((owner, low_publisher, high_publisher, spare_publisher));

    let storage = pool::storage_report();
    let raw_components_bytes = storage.payload_heap_bytes
        + storage.index_heap_bytes
        + storage.bitmap_bytes
        + storage.cursor_bytes;
    assert_eq!(raw_components_bytes, 21_592);
    outcome.attempted = 516;
    outcome.admitted = 514;
    outcome.rejected = 2;
    outcome.handled = 514;
    outcome.endpoints_closed = true;
    outcome.capture_pool = Some(Report {
        scope: "fixture-pooled-scalar-transport",
        pause_stage: "after-slot-claim-before-payload",
        capacity: pool::CAPACITY,
        sources: pool::SOURCES,
        other_source_records: OTHER_RECORDS,
        restart_records: RESTART_RECORDS,
        rejected_full: 1,
        rejected_stale: 1,
        source_order: "per-source-fifo",
        complete_payloads: true,
        sticky_terminal: true,
        clean_restart: true,
        pause_hits: pause.hits.load(SeqCst),
        after_claim: progress.after_claim,
        full: progress.full,
        pending: progress.pending,
        retired: progress.retired,
        restarted: progress.restarted,
        first_missing_source: 1,
        first_missing_sequence: OTHER_RECORDS as u32,
        atomic_16: cfg!(target_has_atomic = "16"),
        atomic_32: cfg!(target_has_atomic = "32"),
        pointer_bytes: std::mem::size_of::<usize>(),
        storage: Storage {
            payload_heap_bytes: storage.payload_heap_bytes,
            index_heap_bytes: storage.index_heap_bytes,
            bitmap_bytes: storage.bitmap_bytes,
            cursor_bytes: storage.cursor_bytes,
            storage_inline_bytes: storage.storage_inline_bytes,
            raw_components_bytes,
            scope: "storage-components-excludes-gate-arc-thread-and-allocator-overhead",
        },
        native_configuration: platform::mutex_configuration(),
        low_after_claim,
        low_after_other_progress,
        owner_waiting,
    });
    outcome
}

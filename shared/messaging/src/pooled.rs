//! Retained, shared-capacity delivery from three exclusive scalar sources.
//!
//! This narrow transport has 512 shared records and three source-owned index
//! lanes. A single source may use all capacity; its lane preserves FIFO order.
//! The owner checks at most three lanes per poll and skips unpublished entries,
//! so a paused producer cannot make the consumer wait for its payload. There is
//! no promised total order between different sources.
//!
//! Setup reserves all storage. Admission, publication and reads neither allocate
//! nor retire a shared allocation. Slots contain scalar atomics, not arbitrary
//! values or destructors. Admission combines epoch, capacity and source ownership
//! in one atomic word; the owner must retire both retained records and in-flight
//! publications before finishing or opening another epoch. Overflow retains the
//! first rejected source/sequence independently of notification delivery.
//!
//! Both endpoints require mutable access and are neither `Clone` nor `Sync`.
//! Move each publisher directly to one acquisition callback and keep the owner
//! in its existing worker. The module creates no worker or wait point. A supplied
//! [`Notify`] adapter controls waking that worker after retained state changes.
//!
//! The arena stays allocated while inactive and is reclaimed on final endpoint
//! drop. [`RetainedBudget`] makes that lifetime and requested-allocation ceiling
//! explicit. It covers the pool's five allocations, including its `Arc` layout;
//! any notifier-owned heap, allocator bookkeeping and thread stacks are separate.
//! [`required_bytes`] includes the notifier's inline size but not its referents.
//!
//! CAS admission and bitmap scans have no per-call SMP retry bound. Notification
//! may use its own synchronization. This API is neither a general wait-free or
//! ISR-safe guarantee nor a claim that arbitrary user callbacks meet deadlines.
//! The qualified fixed layout deliberately has no configurable lane/capacity
//! split. Platform atomics, wake behavior and actual target footprint still need
//! matching evidence. Probe hooks are absent from ordinary builds.

#[cfg(any(test, feature = "probe"))]
#[doc(hidden)]
pub mod gate;
#[cfg(not(any(test, feature = "probe")))]
mod gate;
use gate::{Claim, Gate};
pub use gate::{Key, Rejection, StartError, Status};
use std::boxed::Box;
use std::cell::Cell;
use std::collections::TryReserveError;
use std::fmt;
use std::marker::PhantomData;
use std::mem::size_of;
use std::sync::atomic::{AtomicU16, AtomicU32, AtomicU8, AtomicUsize, Ordering::*};
use std::sync::Arc;
use std::thread::Thread;
use std::vec::Vec;

pub const CAPACITY: usize = 512;
pub const SOURCES: usize = 3;
const WORDS: usize = 9;
pub const PAYLOAD_WORDS: usize = WORDS - 1;

/// Wake an existing execution owner after readiness or terminal state is kept.
///
/// An adapter on a deadline-sensitive path must not allocate, wait for a lock,
/// or panic. It may skip a contended wake if its owner also checks retained
/// state on a bounded deadline. The pool always stores readiness first.
/// Implementations and the target's wake primitive need their own qualification;
/// the trait cannot prove those progress properties for arbitrary user code.
pub trait Notify: Send + Sync {
    fn notify(&self);
}

impl Notify for Thread {
    fn notify(&self) {
        self.unpark();
    }
}

/// Explicit approval of the pool's requested heap while active **and inactive**.
///
/// No epoch releases or replaces the arena. Its final endpoint drop reclaims
/// storage, so stop/join and endpoint teardown belong to the composition owner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RetainedBudget {
    pub max_requested_bytes: usize,
}

#[derive(Debug)]
pub enum SetupError {
    BudgetExceeded { required: usize, maximum: usize },
    Allocation(TryReserveError),
}

impl fmt::Display for SetupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BudgetExceeded { required, maximum } => write!(
                f,
                "retained scalar pool needs {required} requested heap bytes; budget is {maximum}"
            ),
            Self::Allocation(error) => write!(f, "reserve scalar pool: {error}"),
        }
    }
}

impl std::error::Error for SetupError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::BudgetExceeded { .. } => None,
            Self::Allocation(error) => Some(error),
        }
    }
}

// Arc's two counters and repr(C) data alignment. Allocation regression controls
// compare this requested-size calculation with the actual allocator. This is
// not allocator usable-size or metadata accounting; those remain target costs.
#[repr(C)]
struct ArcAllocation<T> {
    strong: AtomicUsize,
    weak: AtomicUsize,
    value: T,
}

/// Requested pool allocation bytes for this notifier and compiled feature set.
///
/// Includes four scalar/index arrays and the shared Arc allocation. Excludes
/// heap allocations referred to by `W`, allocator bookkeeping and worker stacks.
/// Probe builds can have a different shared layout from production builds.
/// The Arc layout calculation is checked against requested allocations on the
/// pinned toolchain; it is not a stable layout promise across Rust versions.
pub fn required_bytes<W: Notify>() -> usize {
    CAPACITY * size_of::<Slot>()
        + SOURCES * CAPACITY * size_of::<AtomicU16>()
        + size_of::<ArcAllocation<Shared<W>>>()
}

/// An independently sequenced record of eight uninterpreted 32-bit words.
/// Source identity belongs to its exclusive publisher, never the payload.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Record {
    pub sequence: u32,
    pub words: [u32; PAYLOAD_WORDS],
}

impl Record {
    fn encoded(self) -> [u32; WORDS] {
        let mut words = [0; WORDS];
        words[0] = self.sequence;
        words[1..].copy_from_slice(&self.words);
        words
    }

    fn from_words(words: [u32; WORDS]) -> Self {
        Self {
            sequence: words[0],
            words: words[1..].try_into().unwrap(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Captured {
    pub source: u8,
    pub record: Record,
}

// Keep the hook vocabulary private in ordinary builds. Public injection is
// available only to the explicit qualification feature and unit tests.
mod hooks {
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub enum Stage {
        AfterSlotClaim,
        MidPayload,
        BeforePublication,
        AfterPublication,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub enum OpenStage {
        BeforeOpen,
        AfterOpen,
    }
}
#[cfg(any(test, feature = "probe"))]
pub use hooks::{OpenStage, Stage};
#[cfg(not(any(test, feature = "probe")))]
use hooks::{OpenStage, Stage};

struct Slot {
    words: [AtomicU32; WORDS],
}

struct Lane {
    indices: Box<[AtomicU16]>,
    published: AtomicU32,
    consumed: AtomicU32,
}

impl Lane {
    fn try_new() -> Result<Self, TryReserveError> {
        // Allocate incrementally so setup never constructs a 21 KiB arena on
        // an acquisition/coordinator stack before copying it into the heap.
        let mut indices = Vec::new();
        indices.try_reserve_exact(CAPACITY)?;
        indices.extend((0..CAPACITY).map(|_| AtomicU16::new(0)));
        Ok(Self {
            indices: indices.into_boxed_slice(),
            published: AtomicU32::new(0),
            consumed: AtomicU32::new(0),
        })
    }

    fn prepare(&self, index: u16) -> u32 {
        // Exactly one source publisher writes this lane. The lifecycle busy
        // bit also rejects re-entry after a reservation was forgotten.
        let published = self.published.load(Relaxed);
        let consumed = self.consumed.load(Acquire);
        assert!(published.wrapping_sub(consumed) < CAPACITY as u32);
        self.indices[published as usize % CAPACITY].store(index, Relaxed);
        published.wrapping_add(1)
    }

    fn publish(&self, next: u32) {
        // Publishes both the index and every scalar field of its payload.
        self.published.store(next, Release);
    }

    fn pop(&self) -> Option<u16> {
        // Exactly one owner advances consumed. Checking a lane does not wait
        // for a producer that has claimed a slot but not published its cursor.
        let consumed = self.consumed.load(Relaxed);
        if consumed == self.published.load(Acquire) {
            return None;
        }
        let index = self.indices[consumed as usize % CAPACITY].load(Relaxed);
        self.consumed.store(consumed.wrapping_add(1), Release);
        Some(index)
    }

    fn published_len(&self) -> usize {
        // Only the owner advances consumed. Producers can only add complete
        // entries, so the owner sees a lower bound that remains available.
        self.published
            .load(Acquire)
            .wrapping_sub(self.consumed.load(Relaxed)) as usize
    }

    fn is_empty(&self) -> bool {
        self.published_len() == 0
    }
}

struct Storage {
    slots: Box<[Slot]>,
    free: [AtomicU32; CAPACITY / 32],
    lanes: [Lane; SOURCES],
}

struct Shared<W: Notify> {
    gate: Gate<W>,
    storage: Storage,
    live_publishers: AtomicU8,
}

/// One source owns this publisher; it cannot be cloned or shared as `Sync`.
///
/// ```compile_fail
/// use messaging::pooled::{channel, RetainedBudget};
/// let (_, [publisher, _, _]) = channel(
///     RetainedBudget { max_requested_bytes: 22 * 1024 },
///     std::thread::current(),
/// ).unwrap();
/// let duplicate = publisher.clone();
/// ```
///
/// ```compile_fail
/// fn require_sync<T: Sync>() {}
/// require_sync::<messaging::pooled::Publisher>();
/// ```
pub struct Publisher<W: Notify = Thread> {
    source: u8,
    shared: Arc<Shared<W>>,
    _exclusive: PhantomData<Cell<()>>,
}

/// The sole consumer and epoch owner. Move it only after its old worker joins.
///
/// ```compile_fail
/// fn require_sync<T: Sync>() {}
/// require_sync::<messaging::pooled::Owner>();
/// ```
///
/// ```compile_fail
/// use messaging::pooled::{channel, RetainedBudget};
/// let (owner, _) = channel(
///     RetainedBudget { max_requested_bytes: 22 * 1024 },
///     std::thread::current(),
/// ).unwrap();
/// let duplicate = owner.clone();
/// ```
pub struct Owner<W: Notify = Thread> {
    shared: Arc<Shared<W>>,
    next_source: usize,
    _exclusive: PhantomData<Cell<()>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Rejected {
    pub reason: Rejection,
    pub record: Record,
}

/// Allocate every record slot and endpoint before starting acquisition.
///
/// `budget` explicitly accepts storage retained while the channel is inactive.
/// Budget rejection happens before any pool allocation. The four scalar/index
/// allocations are fallible; the small final `Arc` allocation follows Rust's
/// global allocator failure behavior, also before any producer can be started.
/// Dropping the final endpoint releases all five allocations.
pub fn channel<W: Notify>(
    budget: RetainedBudget,
    notifier: W,
) -> Result<(Owner<W>, [Publisher<W>; SOURCES]), SetupError> {
    let required = required_bytes::<W>();
    if required > budget.max_requested_bytes {
        return Err(SetupError::BudgetExceeded {
            required,
            maximum: budget.max_requested_bytes,
        });
    }
    let storage = Storage::try_new().map_err(SetupError::Allocation)?;
    let shared = Arc::new(Shared {
        gate: Gate::new(0, notifier),
        storage,
        live_publishers: AtomicU8::new(SOURCES as u8),
    });
    let publishers = std::array::from_fn(|source| Publisher {
        source: source as u8,
        shared: shared.clone(),
        _exclusive: PhantomData,
    });
    Ok((
        Owner {
            shared,
            next_source: 0,
            _exclusive: PhantomData,
        },
        publishers,
    ))
}

impl<W: Notify> Publisher<W> {
    /// Capture before encoding a record, so a paused encoding cannot enter a
    /// later epoch. A closed/inactive epoch is rejected by [`Self::offer`].
    pub fn captured_epoch(&self) -> u32 {
        self.shared.gate.captured_epoch()
    }

    /// Admit and publish without allocating or waiting for occupied storage.
    /// Returns the original record when the captured epoch is closed/stale,
    /// when full admission closes this epoch, or if a source obligation leaked.
    /// The notifier runs only after state needed by the owner is retained.
    pub fn offer(&mut self, captured_epoch: u32, record: Record) -> Result<(), Rejected> {
        self.offer_inner(captured_epoch, record, |_| {})
    }

    /// Qualification hooks run inside the real storage path.
    #[cfg(any(test, feature = "probe"))]
    pub fn offer_with_hooks(
        &mut self,
        captured_epoch: u32,
        record: Record,
        hook: impl FnMut(Stage),
    ) -> Result<(), Rejected> {
        self.offer_inner(captured_epoch, record, hook)
    }

    fn offer_inner(
        &mut self,
        captured_epoch: u32,
        record: Record,
        mut hook: impl FnMut(Stage),
    ) -> Result<(), Rejected> {
        // The public path cannot allocate a slot without first owning a credit.
        // Busy bits also contain a forgotten admission in the shared gate.
        let mut credit =
            match self
                .shared
                .gate
                .reserve(self.source, captured_epoch, record.sequence)
            {
                Claim::Accepted(credit) => credit,
                Claim::Full(closing) => {
                    closing.publish();
                    return Err(Rejected {
                        reason: Rejection::Full,
                        record,
                    });
                }
                Claim::Rejected(reason) => return Err(Rejected { reason, record }),
            };
        // Declaration/drop order matters: any unwind first releases physical
        // storage, then Credit's Drop cancels and releases logical admission.
        let mut slot = SlotGuard {
            storage: &self.shared.storage,
            index: Some(self.shared.storage.claim()),
        };
        let index = slot.index.unwrap();
        hook(Stage::AfterSlotClaim);
        self.shared.storage.write(index, record, &mut hook);
        let lane = &self.shared.storage.lanes[usize::from(self.source)];
        let next = lane.prepare(index);
        hook(Stage::BeforePublication);
        lane.publish(next);
        // There is no user code, hook, allocation, or fallible operation in
        // this transfer. Once cursor publication exposes the record, only the
        // owner returns its physical slot and logical credit, even on unwind.
        slot.index = None;
        credit.mark_published();
        hook(Stage::AfterPublication);
        credit.finish();
        Ok(())
    }
}

impl<W: Notify> Drop for Publisher<W> {
    fn drop(&mut self) {
        if self.shared.live_publishers.fetch_sub(1, AcqRel) == 1 {
            // No future source can publish. Retained records remain available
            // to the owner; disconnection must preserve any overflow marker.
            self.shared.gate.disconnect();
        }
    }
}

struct SlotGuard<'a> {
    storage: &'a Storage,
    index: Option<u16>,
}

impl Drop for SlotGuard<'_> {
    fn drop(&mut self) {
        if let Some(index) = self.index.take() {
            self.storage.release(index);
        }
    }
}

impl<W: Notify> Owner<W> {
    /// Open a new epoch only after the previous epoch was explicitly finished.
    /// Rejects remaining obligations, exhausted epoch space or no live sources.
    pub fn begin(&mut self) -> Result<u32, StartError> {
        self.begin_inner(|_| {})
    }

    /// Qualification boundaries for racing final-source retirement with open.
    #[cfg(any(test, feature = "probe"))]
    pub fn begin_with_hooks(&mut self, hook: impl FnMut(OpenStage)) -> Result<u32, StartError> {
        self.begin_inner(hook)
    }

    fn begin_inner(&mut self, mut hook: impl FnMut(OpenStage)) -> Result<u32, StartError> {
        if self.shared.gate.status() == Status::Inactive {
            assert!(self.shared.storage.is_empty());
            assert_eq!(self.free_slots(), CAPACITY);
            if self.shared.live_publishers.load(Acquire) == 0 {
                return Err(StartError::Disconnected);
            }
            hook(OpenStage::BeforeOpen);
        }
        let epoch = self.shared.gate.begin()?;
        hook(OpenStage::AfterOpen);
        if self.shared.live_publishers.load(Acquire) == 0 {
            // Last-source retirement can race the opening CAS while inactive.
            // If a source published before retiring, preserve that accepted
            // work for the owner to account for; only an empty epoch rolls back.
            self.shared.gate.disconnect();
            if self.shared.gate.obligations() == (0, 0) {
                assert!(self.shared.storage.is_empty());
                assert_eq!(self.free_slots(), CAPACITY);
                self.shared.gate.finish().unwrap();
                return Err(StartError::Disconnected);
            }
        }
        Ok(epoch)
    }

    /// Inspect at most three lanes, without waiting for unpublished payloads.
    pub fn poll(&mut self) -> Option<Captured> {
        for offset in 0..SOURCES {
            let source = (self.next_source + offset) % SOURCES;
            if let Some(index) = self.shared.storage.lanes[source].pop() {
                let record = self.shared.storage.read(index);
                // Physical availability precedes logical capacity. An accepted
                // reservation therefore never has to await an occupied slot.
                self.shared.storage.release(index);
                self.shared.gate.release_credit();
                self.next_source = (source + 1) % SOURCES;
                return Some(Captured {
                    source: source as u8,
                    record,
                });
            }
        }
        None
    }

    /// Close admission immediately. Already accepted records must still be
    /// polled (handled or explicitly discarded) before [`Self::finish`].
    pub fn cancel(&mut self) {
        self.shared.gate.cancel();
    }

    pub fn status(&mut self) -> Status {
        let status = self.shared.gate.status();
        if matches!(status, Status::Overflow(_) | Status::Cancelled) {
            assert!(self.shared.storage.is_empty());
            assert_eq!(self.free_slots(), CAPACITY);
        }
        status
    }

    /// Acknowledge a quiescent terminal and return to inactive without releasing
    /// storage. `Pending` forbids reuse even if the published lanes are empty.
    pub fn finish(&mut self) -> Result<Status, StartError> {
        self.status();
        self.shared.gate.finish()
    }

    /// Published records currently available to this owner, excluding in-flight
    /// reservations. Concurrent producers may add records after this snapshot;
    /// no other owner can remove the records counted here.
    pub fn published_len(&self) -> usize {
        self.shared
            .storage
            .lanes
            .iter()
            .map(Lane::published_len)
            .sum()
    }

    /// Peak occupied credits this epoch, including unpublished reservations.
    /// This resets only when a quiescent new epoch successfully opens.
    /// An active observation can briefly lag an admitted producer's update.
    /// Read after publication obligations retire for an exact epoch peak.
    pub fn high_water(&self) -> usize {
        self.shared.gate.high_water()
    }

    pub fn obligations(&self) -> (u32, u32) {
        self.shared.gate.obligations()
    }

    /// A cross-word diagnostic; use a quiescent checkpoint for an exact count.
    pub fn free_slots(&self) -> usize {
        self.shared.storage.free_slots()
    }

    pub fn take_ready(&mut self) -> bool {
        self.shared.gate.take_ready()
    }
}

impl<W: Notify> Drop for Owner<W> {
    fn drop(&mut self) {
        // Reject future admission immediately. Already admitted publications
        // may still retire; their Arc endpoints keep storage alive. With no
        // consumer, retained scalar records are discarded at final teardown.
        self.shared.gate.cancel();
    }
}

impl Storage {
    fn try_new() -> Result<Self, TryReserveError> {
        let mut slots = Vec::new();
        slots.try_reserve_exact(CAPACITY)?;
        slots.extend((0..CAPACITY).map(|_| Slot {
            words: std::array::from_fn(|_| AtomicU32::new(0)),
        }));
        // Build each small lane owner on the stack; the large arrays already
        // live in their final heap allocations. Partial setup drops cleanly.
        let lanes = [Lane::try_new()?, Lane::try_new()?, Lane::try_new()?];
        Ok(Self {
            slots: slots.into_boxed_slice(),
            free: std::array::from_fn(|_| AtomicU32::new(u32::MAX)),
            lanes,
        })
    }

    // Private: call only while holding an admitted, unpublished reservation.
    // Returning the physical slot before returning its occupied credit makes
    // that reservation proof that there is storage to claim. A scan can miss
    // moving holes under contention, so scan exhaustion is never reported Full.
    // This retry has no per-call bound on SMP; it never awaits a claimed slot.
    fn claim(&self) -> u16 {
        loop {
            for (word_index, word) in self.free.iter().enumerate() {
                let mut available = word.load(Acquire);
                while available != 0 {
                    let bit = available.trailing_zeros();
                    let mask = 1 << bit;
                    let before = word.fetch_and(!mask, AcqRel);
                    if before & mask != 0 {
                        return (word_index * 32 + bit as usize) as u16;
                    }
                    available = before & !mask;
                }
            }
        }
    }

    fn release(&self, index: u16) {
        let index = usize::from(index);
        let mask = 1 << (index % 32);
        let before = self.free[index / 32].fetch_or(mask, Release);
        assert_eq!(before & mask, 0, "physical slot returned twice");
    }

    fn write(&self, index: u16, record: Record, hook: &mut impl FnMut(Stage)) {
        let words = record.encoded();
        for (offset, (target, value)) in self.slots[usize::from(index)]
            .words
            .iter()
            .zip(words)
            .enumerate()
        {
            target.store(value, Relaxed);
            if offset == 3 {
                hook(Stage::MidPayload);
            }
        }
    }

    fn read(&self, index: u16) -> Record {
        Record::from_words(std::array::from_fn(|offset| {
            self.slots[usize::from(index)].words[offset].load(Relaxed)
        }))
    }

    fn free_slots(&self) -> usize {
        self.free
            .iter()
            .map(|word| word.load(Acquire).count_ones() as usize)
            .sum()
    }

    fn is_empty(&self) -> bool {
        self.lanes.iter().all(Lane::is_empty)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StorageReport {
    pub capacity: usize,
    pub payload_heap_bytes: usize,
    pub index_heap_bytes: usize,
    pub bitmap_bytes: usize,
    pub cursor_bytes: usize,
    pub storage_inline_bytes: usize,
    pub requested_heap_bytes: usize,
    pub setup_allocations: usize,
}

pub fn storage_report<W: Notify>() -> StorageReport {
    StorageReport {
        capacity: CAPACITY,
        payload_heap_bytes: CAPACITY * std::mem::size_of::<Slot>(),
        index_heap_bytes: SOURCES * CAPACITY * std::mem::size_of::<AtomicU16>(),
        bitmap_bytes: CAPACITY / 32 * std::mem::size_of::<AtomicU32>(),
        cursor_bytes: SOURCES * 2 * std::mem::size_of::<AtomicU32>(),
        storage_inline_bytes: std::mem::size_of::<Storage>(),
        requested_heap_bytes: required_bytes::<W>(),
        setup_allocations: 5,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    struct CountWake<'a>(&'a AtomicUsize);
    impl Notify for CountWake<'_> {
        fn notify(&self) {
            self.0.fetch_add(1, Relaxed);
        }
    }

    fn budget<W: Notify>() -> RetainedBudget {
        RetainedBudget {
            max_requested_bytes: required_bytes::<W>(),
        }
    }

    fn record(sequence: u32) -> Record {
        Record {
            sequence,
            words: std::array::from_fn(|word| sequence ^ (word as u32).rotate_left(17)),
        }
    }

    #[test]
    fn retained_readiness_and_peak_survive_skipped_wakes_and_failed_begin() {
        let wakes = AtomicUsize::new(0);
        let (mut owner, [mut first, mut second, _third]) =
            channel(budget::<CountWake<'_>>(), CountWake(&wakes)).unwrap();
        let epoch = owner.begin().unwrap();
        first.offer(epoch, record(1)).unwrap();
        second.offer(epoch, record(2)).unwrap();
        assert_eq!(wakes.load(Relaxed), 2);
        assert!(owner.take_ready());
        assert!(!owner.take_ready());
        assert_eq!(owner.published_len(), 2);
        assert_eq!(owner.high_water(), 2);
        assert_eq!(owner.begin(), Err(StartError::Pending));
        assert_eq!(owner.high_water(), 2);
        assert_eq!(owner.poll().unwrap().record, record(1));
        assert_eq!(owner.poll().unwrap().record, record(2));
        owner.cancel();
        assert!(owner.take_ready());
        assert_eq!(owner.finish(), Ok(Status::Cancelled));
        assert_eq!(owner.high_water(), 2);
        let next = owner.begin().unwrap();
        assert_ne!(next, epoch);
        assert_eq!(owner.high_water(), 0);
        assert_eq!(
            first.offer(epoch, record(3)).unwrap_err().reason,
            Rejection::Closed
        );
        assert_eq!(owner.high_water(), 0);
        first.offer(next, record(4)).unwrap();
        assert_eq!(owner.high_water(), 1);
    }

    #[test]
    fn published_length_excludes_reservations_and_cancel_waits_for_retirement() {
        let (mut owner, [mut paused, mut active, _third]) =
            channel(budget::<Thread>(), thread::current()).unwrap();
        let epoch = owner.begin().unwrap();
        let (ready_tx, ready_rx) = mpsc::sync_channel(0);
        let (resume_tx, resume_rx) = mpsc::sync_channel(0);
        let producer = thread::spawn(move || {
            paused
                .offer_with_hooks(epoch, record(1), |stage| {
                    if stage == Stage::BeforePublication {
                        ready_tx.send(()).unwrap();
                        resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    }
                })
                .unwrap();
            paused
        });
        ready_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(owner.published_len(), 0);
        assert_eq!(owner.obligations(), (1, 1));
        assert_eq!(owner.high_water(), 1);
        active.offer(epoch, record(2)).unwrap();
        assert_eq!(owner.published_len(), 1);
        assert_eq!(owner.high_water(), 2);
        assert_eq!(owner.poll().unwrap().record, record(2));
        assert_eq!(owner.published_len(), 0);
        owner.cancel();
        assert_eq!(owner.status(), Status::Pending);
        assert_eq!(owner.finish(), Err(StartError::Pending));
        assert_eq!(owner.begin(), Err(StartError::Pending));
        resume_tx.send(()).unwrap();
        let _paused = producer.join().unwrap();
        assert_eq!(owner.published_len(), 1);
        assert_eq!(owner.poll().unwrap().record, record(1));
        assert_eq!(owner.obligations(), (0, 0));
        assert_eq!(owner.finish(), Ok(Status::Cancelled));
    }
}

//! Fixture-only pooled scalar transport. No product migration or timing claim.
#![forbid(unsafe_code)]

use super::capture_gate::{Claim, Gate};
pub(crate) use super::capture_gate::{Key, Rejection, StartError, Status};
use std::cell::Cell;
use std::marker::PhantomData;
use std::sync::Arc;
use std::sync::atomic::{AtomicU8, AtomicU16, AtomicU32, Ordering::*};
use std::thread::Thread;

pub const CAPACITY: usize = 512;
pub const SOURCES: usize = 3;
const WORDS: usize = 9;

/// The capture fields are preserved bit-for-bit; the endpoint identifies source.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Record {
    pub time_ms: u64,
    pub sequence: u32,
    pub values: [u32; 6],
}

impl Record {
    fn words(self) -> [u32; WORDS] {
        [
            self.time_ms as u32,
            (self.time_ms >> 32) as u32,
            self.sequence,
            self.values[0],
            self.values[1],
            self.values[2],
            self.values[3],
            self.values[4],
            self.values[5],
        ]
    }

    fn from_words(words: [u32; WORDS]) -> Self {
        Self {
            time_ms: u64::from(words[0]) | (u64::from(words[1]) << 32),
            sequence: words[2],
            values: words[3..].try_into().unwrap(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Captured {
    pub source: u8,
    pub record: Record,
}

/// Qualification hooks run inside the real storage path, never in production.
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

struct Slot {
    words: [AtomicU32; WORDS],
}

struct Lane {
    indices: Box<[AtomicU16]>,
    published: AtomicU32,
    consumed: AtomicU32,
}

impl Lane {
    fn new() -> Self {
        // Allocate incrementally so setup never constructs a 21 KiB arena on
        // an acquisition/coordinator stack before copying it into the heap.
        let mut indices = Vec::with_capacity(CAPACITY);
        indices.extend((0..CAPACITY).map(|_| AtomicU16::new(0)));
        Self {
            indices: indices.into_boxed_slice(),
            published: AtomicU32::new(0),
            consumed: AtomicU32::new(0),
        }
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

    fn is_empty(&self) -> bool {
        self.consumed.load(Acquire) == self.published.load(Acquire)
    }
}

struct Storage {
    slots: Box<[Slot]>,
    free: [AtomicU32; CAPACITY / 32],
    lanes: [Lane; SOURCES],
}

struct Shared {
    gate: Gate,
    storage: Storage,
    live_publishers: AtomicU8,
}

/// One source owns each publisher. Neither endpoint is Clone or Sync.
pub struct Publisher {
    source: u8,
    shared: Arc<Shared>,
    _exclusive: PhantomData<Cell<()>>,
}

pub struct Owner {
    shared: Arc<Shared>,
    next_source: usize,
    _exclusive: PhantomData<Cell<()>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Rejected {
    pub reason: Rejection,
    pub record: Record,
}

pub fn channel(owner: Thread) -> (Owner, [Publisher; SOURCES]) {
    let shared = Arc::new(Shared {
        gate: Gate::new(0, owner),
        storage: Storage::new(),
        live_publishers: AtomicU8::new(SOURCES as u8),
    });
    let publishers = std::array::from_fn(|source| Publisher {
        source: source as u8,
        shared: shared.clone(),
        _exclusive: PhantomData,
    });
    (
        Owner {
            shared,
            next_source: 0,
            _exclusive: PhantomData,
        },
        publishers,
    )
}

impl Publisher {
    pub fn captured_epoch(&self) -> u32 {
        self.shared.gate.captured_epoch()
    }

    pub fn offer(&mut self, captured_epoch: u32, record: Record) -> Result<(), Rejected> {
        self.offer_with_hooks(captured_epoch, record, |_| {})
    }

    pub fn offer_with_hooks(
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

impl Drop for Publisher {
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

impl Owner {
    pub fn begin(&mut self) -> Result<u32, StartError> {
        self.begin_with_hooks(|_| {})
    }

    /// Fixture-only boundaries for racing final-source retirement with open.
    pub fn begin_with_hooks(&mut self, mut hook: impl FnMut(OpenStage)) -> Result<u32, StartError> {
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

    pub fn finish(&mut self) -> Result<Status, StartError> {
        self.status();
        self.shared.gate.finish()
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

impl Drop for Owner {
    fn drop(&mut self) {
        // Reject future admission immediately. Already admitted publications
        // may still retire; their Arc endpoints keep storage alive. With no
        // consumer, retained scalar records are discarded at final teardown.
        self.shared.gate.cancel();
    }
}

impl Storage {
    fn new() -> Self {
        let mut slots = Vec::with_capacity(CAPACITY);
        slots.extend((0..CAPACITY).map(|_| Slot {
            words: std::array::from_fn(|_| AtomicU32::new(0)),
        }));
        Self {
            slots: slots.into_boxed_slice(),
            free: std::array::from_fn(|_| AtomicU32::new(u32::MAX)),
            lanes: std::array::from_fn(|_| Lane::new()),
        }
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
        let words = record.words();
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
}

pub fn storage_report() -> StorageReport {
    StorageReport {
        capacity: CAPACITY,
        payload_heap_bytes: CAPACITY * std::mem::size_of::<Slot>(),
        index_heap_bytes: SOURCES * CAPACITY * std::mem::size_of::<AtomicU16>(),
        bitmap_bytes: CAPACITY / 32 * std::mem::size_of::<AtomicU32>(),
        cursor_bytes: SOURCES * 2 * std::mem::size_of::<AtomicU32>(),
        storage_inline_bytes: std::mem::size_of::<Storage>(),
    }
}

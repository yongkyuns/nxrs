//! Fixture-only shared admission and retirement, independent of payload storage.
//! Payload adapters must publish before retiring a credit's source obligation,
//! and return physical storage before returning its logical credit.
#![forbid(unsafe_code)]

use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU32, AtomicUsize, Ordering::*};
use std::thread::Thread;

pub(crate) const CAPACITY: u32 = 512;
pub(crate) const MAX_EPOCH: u32 = (1 << 17) - 1;
const OCCUPIED_SHIFT: u32 = 2;
const OCCUPIED_MASK: u32 = 1023 << OCCUPIED_SHIFT;
const BUSY_SHIFT: u32 = 12;
const BUSY_MASK: u32 = 7 << BUSY_SHIFT;
const EPOCH_SHIFT: u32 = 15;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u32)]
pub(crate) enum Phase {
    Inactive,
    Open,
    Overflow,
    Cancelled,
}

fn phase(word: u32) -> Phase {
    match word & 3 {
        0 => Phase::Inactive,
        1 => Phase::Open,
        2 => Phase::Overflow,
        _ => Phase::Cancelled,
    }
}
fn occupied(word: u32) -> u32 {
    (word & OCCUPIED_MASK) >> OCCUPIED_SHIFT
}
fn epoch(word: u32) -> u32 {
    word >> EPOCH_SHIFT
}
fn busy(source: u8) -> u32 {
    1 << (BUSY_SHIFT + u32::from(source))
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct Key {
    pub source: u8,
    pub sequence: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Rejection {
    Full,
    Closed,
    Busy,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum StartError {
    Pending,
    Exhausted,
    Disconnected,
    OutOfMemory,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Status {
    Inactive,
    Open,
    Pending,
    Overflow(Key),
    Cancelled,
}

pub(crate) enum Claim<'a> {
    Accepted(Credit<'a>),
    Full(Closing<'a>),
    Rejected(Rejection),
}

/// The adapters construct one owner and three exclusive publisher endpoints.
/// Busy source bits also contain forgotten permits; a leaked permit may strand
/// the epoch, but it cannot wrap an active count or permit early storage reuse.
pub(crate) struct Gate {
    word: AtomicU32,
    missing_sequence: AtomicU32,
    missing_source: AtomicU8,
    ready: AtomicBool,
    owner: Thread,
    wake_calls: AtomicUsize,
}

impl Gate {
    pub(crate) fn new(initial_epoch: u32, owner: Thread) -> Self {
        assert!(initial_epoch <= MAX_EPOCH);
        Self {
            word: AtomicU32::new(initial_epoch << EPOCH_SHIFT),
            missing_sequence: AtomicU32::new(0),
            missing_source: AtomicU8::new(0),
            ready: AtomicBool::new(false),
            owner,
            wake_calls: AtomicUsize::new(0),
        }
    }

    pub(crate) fn captured_epoch(&self) -> u32 {
        epoch(self.word.load(Acquire))
    }
    pub(crate) fn phase(&self) -> Phase {
        phase(self.word.load(Acquire))
    }
    pub(crate) fn obligations(&self) -> (u32, u32) {
        let word = self.word.load(Acquire);
        (occupied(word), (word & BUSY_MASK) >> BUSY_SHIFT)
    }
    pub(crate) fn take_ready(&self) -> bool {
        self.ready.swap(false, AcqRel)
    }
    pub(crate) fn wake_count(&self) -> usize {
        self.wake_calls.load(SeqCst)
    }

    // The marker is authoritative only after quiescent Overflow status. The
    // paused-marker witness also observes None before its closing publication.
    pub(crate) fn marker(&self) -> Option<Key> {
        let source = self.missing_source.load(Acquire);
        (source != 0).then(|| Key {
            source: source - 1,
            sequence: self.missing_sequence.load(Relaxed),
        })
    }

    pub(crate) fn begin(&self) -> Result<u32, StartError> {
        let before = self.word.load(Acquire);
        if phase(before) != Phase::Inactive || occupied(before) != 0 || before & BUSY_MASK != 0 {
            return Err(StartError::Pending);
        }
        if epoch(before) == MAX_EPOCH {
            return Err(StartError::Exhausted);
        }
        // The exclusive owner checked the adapter is empty and still attached.
        // No previous producer remains entitled to mutate this epoch's state.
        self.missing_source.store(0, Relaxed);
        self.missing_sequence.store(0, Relaxed);
        self.ready.store(false, Relaxed);
        let next = epoch(before) + 1;
        self.word
            .compare_exchange(
                before,
                (next << EPOCH_SHIFT) | Phase::Open as u32,
                AcqRel,
                Acquire,
            )
            .unwrap();
        Ok(next)
    }

    pub(crate) fn reserve(&self, source: u8, captured_epoch: u32, sequence: u32) -> Claim<'_> {
        self.reserve_with(source, captured_epoch, sequence, || {})
    }

    // The hook is a fixture schedule boundary, not a proposed production API.
    pub(crate) fn reserve_with(
        &self,
        source: u8,
        captured_epoch: u32,
        sequence: u32,
        before_compare: impl FnOnce(),
    ) -> Claim<'_> {
        assert!(source < 3);
        let mut before_compare = Some(before_compare);
        let mut before = self.word.load(Acquire);
        loop {
            assert!(occupied(before) <= CAPACITY);
            if epoch(before) != captured_epoch || phase(before) != Phase::Open {
                return Claim::Rejected(Rejection::Closed);
            }
            if before & busy(source) != 0 {
                return Claim::Rejected(Rejection::Busy);
            }
            let full = occupied(before) == CAPACITY;
            let after = if full {
                ((before & !3) | Phase::Overflow as u32) | busy(source)
            } else {
                (before + (1 << OCCUPIED_SHIFT)) | busy(source)
            };
            if let Some(hook) = before_compare.take() {
                hook();
            }
            match self
                .word
                .compare_exchange_weak(before, after, AcqRel, Acquire)
            {
                Ok(_) if full => {
                    return Claim::Full(Closing {
                        gate: self,
                        epoch: captured_epoch,
                        key: Key { source, sequence },
                        active: true,
                    });
                }
                Ok(_) => {
                    return Claim::Accepted(Credit {
                        gate: self,
                        source,
                        epoch: captured_epoch,
                        published: false,
                        active: true,
                        notify_pending: false,
                    });
                }
                Err(changed) => before = changed,
            }
        }
    }

    fn retire(&self, source: u8, admitted_epoch: u32, release_credit: bool, cancel: bool) {
        let mut before = self.word.load(Acquire);
        loop {
            assert_eq!(epoch(before), admitted_epoch);
            assert_ne!(before & busy(source), 0);
            let mut after = before & !busy(source);
            if release_credit {
                assert!(occupied(before) > 0);
                after -= 1 << OCCUPIED_SHIFT;
            }
            if cancel {
                after = (after & !3) | Phase::Cancelled as u32;
            }
            match self
                .word
                .compare_exchange_weak(before, after, AcqRel, Acquire)
            {
                Ok(_) => return,
                Err(changed) => before = changed,
            }
        }
    }

    /// Call only after the consumer has returned the record's physical slot.
    /// A reserved producer can then always find backing storage for its credit.
    pub(crate) fn release_credit(&self) {
        let mut before = self.word.load(Acquire);
        loop {
            assert!(occupied(before) > 0 && occupied(before) <= CAPACITY);
            match self.word.compare_exchange_weak(
                before,
                before - (1 << OCCUPIED_SHIFT),
                AcqRel,
                Acquire,
            ) {
                Ok(_) => return,
                Err(changed) => before = changed,
            }
        }
    }

    pub(crate) fn cancel(&self) {
        let mut before = self.word.load(Acquire);
        loop {
            if matches!(phase(before), Phase::Inactive | Phase::Cancelled) {
                return;
            }
            match self.word.compare_exchange_weak(
                before,
                (before & !3) | Phase::Cancelled as u32,
                AcqRel,
                Acquire,
            ) {
                Ok(_) => {
                    self.notify();
                    return;
                }
                Err(changed) => before = changed,
            }
        }
    }

    /// The last publisher closes an open epoch without replacing an already
    /// retained terminal. Notify even when inactive: endpoint lifetime changed,
    /// and the exclusive owner rechecks that lifetime after opening an epoch.
    pub(crate) fn disconnect(&self) {
        let mut before = self.word.load(Acquire);
        while phase(before) == Phase::Open {
            match self.word.compare_exchange_weak(
                before,
                (before & !3) | Phase::Cancelled as u32,
                AcqRel,
                Acquire,
            ) {
                Ok(_) => break,
                Err(changed) => before = changed,
            }
        }
        self.notify();
    }

    pub(crate) fn status(&self) -> Status {
        let before = self.word.load(Acquire);
        assert!(occupied(before) <= CAPACITY);
        match phase(before) {
            Phase::Inactive => Status::Inactive,
            Phase::Open => Status::Open,
            _ if occupied(before) != 0 || before & BUSY_MASK != 0 => Status::Pending,
            Phase::Overflow => Status::Overflow(
                self.marker()
                    .expect("terminal before missing-marker publication"),
            ),
            Phase::Cancelled => Status::Cancelled,
        }
    }

    pub(crate) fn finish(&self) -> Result<Status, StartError> {
        let status = self.status();
        if !matches!(status, Status::Overflow(_) | Status::Cancelled) {
            return Err(StartError::Pending);
        }
        let before = self.word.load(Acquire);
        assert_eq!(occupied(before), 0);
        assert_eq!(before & BUSY_MASK, 0);
        self.word
            .compare_exchange(before, before & !3, AcqRel, Acquire)
            .unwrap();
        Ok(status)
    }

    fn wake_owner(&self) {
        self.owner.unpark();
        self.wake_calls.fetch_add(1, SeqCst);
    }
    fn notify(&self) {
        self.ready.store(true, Release);
        self.wake_owner();
    }
}

/// A non-cloneable accepted obligation. Adapters return their physical slot
/// before dropping an unpublished credit; published records retain their credit
/// until the consumer copies them and returns that slot.
pub(crate) struct Credit<'a> {
    gate: &'a Gate,
    source: u8,
    epoch: u32,
    published: bool,
    active: bool,
    notify_pending: bool,
}
impl Credit<'_> {
    pub(crate) fn mark_published(&mut self) {
        self.published = true;
    }
    pub(crate) fn finish(self) {
        self.finish_with(|| {});
    }
    pub(crate) fn finish_with(mut self, after_ready: impl FnOnce()) {
        assert!(
            self.published,
            "cannot retire an unpublished record as committed"
        );
        self.gate.retire(self.source, self.epoch, false, false);
        self.active = false;
        self.notify_pending = true;
        self.gate.ready.store(true, Release);
        after_ready();
        self.gate.wake_owner();
        self.notify_pending = false;
    }
}
impl Drop for Credit<'_> {
    fn drop(&mut self) {
        if self.active {
            self.gate
                .retire(self.source, self.epoch, !self.published, true);
            self.gate.notify();
        } else if self.notify_pending {
            self.gate.notify();
        }
    }
}

pub(crate) struct Closing<'a> {
    gate: &'a Gate,
    epoch: u32,
    key: Key,
    active: bool,
}
impl Closing<'_> {
    pub(crate) fn publish(mut self) {
        self.gate.missing_sequence.store(self.key.sequence, Relaxed);
        self.gate.missing_source.store(self.key.source + 1, Release);
        self.gate.retire(self.key.source, self.epoch, false, false);
        self.active = false;
        self.gate.notify();
    }
}
impl Drop for Closing<'_> {
    fn drop(&mut self) {
        if self.active {
            self.gate.retire(self.key.source, self.epoch, false, true);
            self.gate.notify();
        }
    }
}

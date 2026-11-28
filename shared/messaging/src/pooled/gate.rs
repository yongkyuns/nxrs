//! Shared admission and retirement, independent of scalar payload storage.
//! Payload adapters must publish before retiring a credit's source obligation,
//! and return physical storage before returning its logical credit.
#![forbid(unsafe_code)]

#[cfg(any(test, feature = "probe"))]
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU8, Ordering::*};
use std::thread::Thread;

use super::Notify;

pub const CAPACITY: u32 = 512;
pub const MAX_EPOCH: u32 = (1 << 17) - 1;
const OCCUPIED_SHIFT: u32 = 2;
const OCCUPIED_MASK: u32 = 1023 << OCCUPIED_SHIFT;
const BUSY_SHIFT: u32 = 12;
const BUSY_MASK: u32 = 7 << BUSY_SHIFT;
const EPOCH_SHIFT: u32 = 15;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum Phase {
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
pub struct Key {
    pub source: u8,
    pub sequence: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Rejection {
    Full,
    Closed,
    Busy,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StartError {
    Pending,
    Exhausted,
    Disconnected,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Status {
    Inactive,
    Open,
    Pending,
    Overflow(Key),
    Cancelled,
}

pub enum Claim<'a, W: Notify = Thread> {
    Accepted(Credit<'a, W>),
    Full(Closing<'a, W>),
    Rejected(Rejection),
}

/// The adapters construct one owner and three exclusive publisher endpoints.
/// Busy source bits also contain forgotten permits; a leaked permit may strand
/// the epoch, but it cannot wrap an active count or permit early storage reuse.
pub struct Gate<W: Notify = Thread> {
    word: AtomicU32,
    missing_sequence: AtomicU32,
    missing_source: AtomicU8,
    ready: AtomicBool,
    notifier: W,
    high_water: AtomicU32,
    #[cfg(any(test, feature = "probe"))]
    wake_calls: AtomicUsize,
}

impl<W: Notify> Gate<W> {
    pub fn new(initial_epoch: u32, notifier: W) -> Self {
        assert!(initial_epoch <= MAX_EPOCH);
        Self {
            word: AtomicU32::new(initial_epoch << EPOCH_SHIFT),
            missing_sequence: AtomicU32::new(0),
            missing_source: AtomicU8::new(0),
            ready: AtomicBool::new(false),
            notifier,
            high_water: AtomicU32::new(0),
            #[cfg(any(test, feature = "probe"))]
            wake_calls: AtomicUsize::new(0),
        }
    }

    pub fn captured_epoch(&self) -> u32 {
        epoch(self.word.load(Acquire))
    }
    #[cfg(any(test, feature = "probe"))]
    pub fn phase(&self) -> Phase {
        phase(self.word.load(Acquire))
    }
    pub fn obligations(&self) -> (u32, u32) {
        let word = self.word.load(Acquire);
        (occupied(word), (word & BUSY_MASK) >> BUSY_SHIFT)
    }
    pub fn high_water(&self) -> usize {
        self.high_water.load(Acquire) as usize
    }
    pub fn take_ready(&self) -> bool {
        self.ready.swap(false, AcqRel)
    }
    #[cfg(any(test, feature = "probe"))]
    pub fn wake_count(&self) -> usize {
        self.wake_calls.load(SeqCst)
    }

    // The marker is authoritative only after quiescent Overflow status. The
    // paused-marker witness also observes None before its closing publication.
    pub fn marker(&self) -> Option<Key> {
        let source = self.missing_source.load(Acquire);
        (source != 0).then(|| Key {
            source: source - 1,
            sequence: self.missing_sequence.load(Relaxed),
        })
    }

    pub fn begin(&self) -> Result<u32, StartError> {
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
        self.high_water.store(0, Relaxed);
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

    pub fn reserve(&self, source: u8, captured_epoch: u32, sequence: u32) -> Claim<'_, W> {
        self.reserve_inner(source, captured_epoch, sequence, || {})
    }

    /// Qualification schedule boundary; absent from the normal feature set.
    #[cfg(any(test, feature = "probe"))]
    pub fn reserve_with(
        &self,
        source: u8,
        captured_epoch: u32,
        sequence: u32,
        before_compare: impl FnOnce(),
    ) -> Claim<'_, W> {
        self.reserve_inner(source, captured_epoch, sequence, before_compare)
    }

    fn reserve_inner(
        &self,
        source: u8,
        captured_epoch: u32,
        sequence: u32,
        before_compare: impl FnOnce(),
    ) -> Claim<'_, W> {
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
                    self.record_high_water(occupied(after));
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

    fn record_high_water(&self, depth: u32) {
        // The pinned Xtensa compiler miscompiles optimized atomic min/max.
        // Keep the same maximum with the already-qualified CAS primitive.
        let mut previous = self.high_water.load(Relaxed);
        while previous < depth {
            match self
                .high_water
                .compare_exchange_weak(previous, depth, Relaxed, Relaxed)
            {
                Ok(_) => break,
                Err(current) => previous = current,
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
    pub fn release_credit(&self) {
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

    pub fn cancel(&self) {
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
    pub fn disconnect(&self) {
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

    pub fn status(&self) -> Status {
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

    pub fn finish(&self) -> Result<Status, StartError> {
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
        self.notifier.notify();
        #[cfg(any(test, feature = "probe"))]
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
pub struct Credit<'a, W: Notify = Thread> {
    gate: &'a Gate<W>,
    source: u8,
    epoch: u32,
    published: bool,
    active: bool,
    notify_pending: bool,
}
impl<W: Notify> Credit<'_, W> {
    pub fn mark_published(&mut self) {
        self.published = true;
    }
    pub fn finish(self) {
        self.finish_inner(|| {});
    }
    /// Qualification schedule boundary; absent from the normal feature set.
    #[cfg(any(test, feature = "probe"))]
    pub fn finish_with(self, after_ready: impl FnOnce()) {
        self.finish_inner(after_ready);
    }
    fn finish_inner(mut self, after_ready: impl FnOnce()) {
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
impl<W: Notify> Drop for Credit<'_, W> {
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

pub struct Closing<'a, W: Notify = Thread> {
    gate: &'a Gate<W>,
    epoch: u32,
    key: Key,
    active: bool,
}
impl<W: Notify> Closing<'_, W> {
    pub fn publish(mut self) {
        self.gate.missing_sequence.store(self.key.sequence, Relaxed);
        self.gate.missing_source.store(self.key.source + 1, Release);
        self.gate.retire(self.key.source, self.epoch, false, false);
        self.active = false;
        self.gate.notify();
    }
}
impl<W: Notify> Drop for Closing<'_, W> {
    fn drop(&mut self) {
        if self.active {
            self.gate.retire(self.key.source, self.epoch, false, true);
            self.gate.notify();
        }
    }
}

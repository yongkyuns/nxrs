//! Diagnostic allocator for isolated tests, never a production dependency.
//!
//! Counts requested Rust allocation bytes, not OS heap usage or resident RAM.
//! No logging, allocation, mutex, TLS, or panic occurs in the allocator hooks.
//! Snapshot retries while hooks are active; this is NOT real-time instrumentation.
#![deny(unsafe_op_in_unsafe_fn)]

use std::alloc::GlobalAlloc;
use std::alloc::Layout;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst};

pub struct Tracking<A> {
    inner: A,
    active: AtomicUsize,
    revision: AtomicUsize,
    invalid: AtomicBool,
    alloc: AtomicUsize,
    zeroed: AtomicUsize,
    realloc: AtomicUsize,
    dealloc: AtomicUsize,
    failed: AtomicUsize,
    blocks: AtomicUsize,
    live: AtomicUsize,
    peak: AtomicUsize,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Snapshot {
    pub alloc: usize,
    pub zeroed: usize,
    pub realloc: usize,
    pub dealloc: usize,
    pub failed: usize,
    pub blocks: usize,
    pub live: usize,
    /// Process/probe lifetime peak of requested live bytes. Never reset by a window.
    pub peak: usize,
    pub invalid: bool,
}

impl<A> Tracking<A> {
    pub const fn new(inner: A) -> Self {
        Self {
            inner,
            active: AtomicUsize::new(0),
            revision: AtomicUsize::new(0),
            invalid: AtomicBool::new(false),
            alloc: AtomicUsize::new(0),
            zeroed: AtomicUsize::new(0),
            realloc: AtomicUsize::new(0),
            dealloc: AtomicUsize::new(0),
            failed: AtomicUsize::new(0),
            blocks: AtomicUsize::new(0),
            live: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
        }
    }

    fn add(&self, counter: &AtomicUsize, amount: usize) -> usize {
        match counter.fetch_update(SeqCst, SeqCst, |value| value.checked_add(amount)) {
            Ok(previous) => previous + amount,
            Err(value) => {
                self.invalid.store(true, SeqCst);
                value
            }
        }
    }

    fn subtract(&self, counter: &AtomicUsize, amount: usize) {
        if counter
            .fetch_update(SeqCst, SeqCst, |value| value.checked_sub(amount))
            .is_err()
        {
            self.invalid.store(true, SeqCst);
        }
    }

    fn enter(&self) {
        self.add(&self.active, 1);
    }

    fn leave(&self) {
        self.add(&self.revision, 1);
        self.subtract(&self.active, 1);
    }

    fn allocated(&self, pointer: *mut u8, size: usize) {
        if pointer.is_null() {
            self.add(&self.failed, 1);
        } else {
            self.add(&self.blocks, 1);
            let live = self.add(&self.live, size);
            self.peak.fetch_max(live, SeqCst);
        }
    }

    /// Coherent across concurrently running hooks, without serializing allocators.
    /// Never call from an allocator hook. May wait for an in-progress OS allocation.
    /// Counter overflow/underflow permanently invalidates subsequent measurements.
    pub fn snapshot(&self) -> Snapshot {
        loop {
            let revision = self.revision.load(SeqCst);
            if self.active.load(SeqCst) != 0 {
                std::hint::spin_loop();
                continue;
            }
            let sample = Snapshot {
                alloc: self.alloc.load(SeqCst),
                zeroed: self.zeroed.load(SeqCst),
                realloc: self.realloc.load(SeqCst),
                dealloc: self.dealloc.load(SeqCst),
                failed: self.failed.load(SeqCst),
                blocks: self.blocks.load(SeqCst),
                live: self.live.load(SeqCst),
                peak: self.peak.load(SeqCst),
                invalid: self.invalid.load(SeqCst),
            };
            if self.active.load(SeqCst) == 0 && self.revision.load(SeqCst) == revision {
                // Saturated revisions stop changing. Recheck sticky invalidity
                // after the coherence checks so saturation cannot look valid.
                return Snapshot {
                    invalid: sample.invalid || self.invalid.load(SeqCst),
                    ..sample
                };
            }
        }
    }
}

// SAFETY: Every allocation/layout/pointer operation is forwarded unchanged to A.
// The counters neither dereference nor retain pointers. Checked arithmetic and
// atomic operations do not allocate or unwind. A failed realloc keeps the old
// allocation live; successful realloc changes its size without changing block count.
unsafe impl<A: GlobalAlloc> GlobalAlloc for Tracking<A> {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        self.enter();
        self.add(&self.alloc, 1);
        // SAFETY: caller supplied a valid nonzero allocation layout.
        let pointer = unsafe { self.inner.alloc(layout) };
        self.allocated(pointer, layout.size());
        self.leave();
        pointer
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        self.enter();
        self.add(&self.zeroed, 1);
        // SAFETY: caller supplied a valid nonzero allocation layout.
        let pointer = unsafe { self.inner.alloc_zeroed(layout) };
        self.allocated(pointer, layout.size());
        self.leave();
        pointer
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        self.enter();
        self.add(&self.dealloc, 1);
        self.subtract(&self.blocks, 1);
        self.subtract(&self.live, layout.size());
        // SAFETY: caller owns this allocation from A with exactly this layout.
        unsafe { self.inner.dealloc(pointer, layout) };
        self.leave();
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        self.enter();
        self.add(&self.realloc, 1);
        // SAFETY: caller owns pointer/layout and supplied a valid nonzero new size.
        let resized = unsafe { self.inner.realloc(pointer, layout, size) };
        if resized.is_null() {
            self.add(&self.failed, 1);
        } else if size >= layout.size() {
            let live = self.add(&self.live, size - layout.size());
            self.peak.fetch_max(live, SeqCst);
        } else {
            self.subtract(&self.live, layout.size() - size);
        }
        self.leave();
        resized
    }
}

impl Snapshot {
    pub fn validate_after(self, before: Self) -> Result<(), &'static str> {
        if self.invalid || before.invalid {
            return Err("allocator counter overflow/underflow");
        }
        if self.alloc < before.alloc
            || self.zeroed < before.zeroed
            || self.realloc < before.realloc
            || self.dealloc < before.dealloc
            || self.failed < before.failed
            || self.peak < before.peak
            || self.peak < self.live
        {
            return Err("non-monotonic or invalid snapshot");
        }
        if self.failed != before.failed {
            return Err("allocation failed during measured workload");
        }
        Ok(())
    }

    pub fn no_allocator_calls_since(self, before: Self) -> Result<(), &'static str> {
        self.validate_after(before)?;
        if self.alloc != before.alloc
            || self.zeroed != before.zeroed
            || self.realloc != before.realloc
            || self.dealloc != before.dealloc
        {
            return Err("allocator activity in a zero-call window");
        }
        Ok(())
    }

    /// Check net requested-byte and block stability, not allocation identities.
    pub fn reclaimed_since(self, before: Self) -> Result<(), &'static str> {
        self.validate_after(before)?;
        if self.live != before.live || self.blocks != before.blocks {
            return Err("owned allocations did not return to baseline");
        }
        Ok(())
    }

    /// Emit only after the entire measured case has ended, never between snapshots.
    pub fn report(self, before: Self, app: &str, case: &str, phase: &str) {
        self.validate_after(before).unwrap();
        for label in [app, case, phase] {
            assert!(label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-'));
        }
        println!(
            "MEMORY_RESULT {{\"app\":\"{app}\",\"case\":\"{case}\",\"phase\":\"{phase}\",\"alloc\":{},\"zeroed\":{},\"realloc\":{},\"dealloc\":{},\"failed\":{},\"live_before\":{},\"live_after\":{},\"blocks_before\":{},\"blocks_after\":{},\"peak_process_requested_bytes\":{},\"valid\":true}}",
            self.alloc - before.alloc, self.zeroed - before.zeroed,
            self.realloc - before.realloc, self.dealloc - before.dealloc,
            self.failed - before.failed, before.live, self.live, before.blocks,
            self.blocks, self.peak
        );
    }
}

/// Initialize output before measurement. Call only from the dedicated
/// harness-free test executable: even single-test
/// libtest execution has a concurrent harness thread that can allocate.
pub fn begin_measurement() {
    // Initialize output before any measured window. Report printing is outside it.
    println!("\nMEMORY_BEGIN scope=rust-global-allocator timing=not-a-benchmark");
}

#[cfg(test)]
mod tests;

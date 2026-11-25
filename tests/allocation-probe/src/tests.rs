use super::*;
use std::alloc::System;
use std::ptr::null_mut;

// Small valid requests fail deterministically, without exhausting physical RAM.
struct FailRealloc;
unsafe impl GlobalAlloc for FailRealloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        unsafe { System.alloc(layout) }
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) }
    }
    unsafe fn realloc(&self, _: *mut u8, _: Layout, _: usize) -> *mut u8 {
        null_mut()
    }
}
struct FailAll;
unsafe impl GlobalAlloc for FailAll {
    unsafe fn alloc(&self, _: Layout) -> *mut u8 {
        null_mut()
    }
    unsafe fn dealloc(&self, _: *mut u8, _: Layout) {}
}

#[test]
fn forwards_alignment_zeroing_growth_shrink_and_release() {
    let probe = Tracking::new(System);
    let initial = probe.snapshot();
    let small = Layout::from_size_align(32, 64).unwrap();
    // SAFETY: all layouts are nonzero/valid and each successful pointer is used
    // only until its next successful realloc or dealloc, with the matching layout.
    unsafe {
        let pointer = probe.alloc_zeroed(small);
        assert!(!pointer.is_null());
        assert_eq!(pointer as usize % 64, 0);
        assert!((0..32).all(|index| *pointer.add(index) == 0));
        *pointer = 91;
        let larger = probe.realloc(pointer, small, 128);
        assert!(!larger.is_null());
        assert_eq!(*larger, 91);
        assert_eq!(larger as usize % 64, 0);
        let large = Layout::from_size_align(128, 64).unwrap();
        let smaller = probe.realloc(larger, large, 16);
        assert!(!smaller.is_null());
        assert_eq!(*smaller, 91);
        let held = probe.snapshot();
        assert_eq!((held.live, held.blocks, held.peak), (16, 1, 128));
        probe.dealloc(smaller, Layout::from_size_align(16, 64).unwrap());
    }
    let end = probe.snapshot();
    end.reclaimed_since(initial).unwrap();
    assert_eq!((end.zeroed, end.realloc, end.dealloc), (1, 2, 1));
}

#[test]
fn failed_reallocation_preserves_original_ownership_and_bytes() {
    let probe = Tracking::new(FailRealloc);
    let layout = Layout::from_size_align(32, 8).unwrap();
    // SAFETY: the failed realloc leaves the original pointer/layout owned and valid.
    unsafe {
        let pointer = probe.alloc(layout);
        assert!(!pointer.is_null());
        *pointer = 77;
        assert!(probe.realloc(pointer, layout, 64).is_null());
        assert_eq!(*pointer, 77);
        let held = probe.snapshot();
        assert_eq!(
            (held.live, held.blocks, held.failed, held.peak),
            (32, 1, 1, 32)
        );
        probe.dealloc(pointer, layout);
    }
    let end = probe.snapshot();
    assert_eq!((end.live, end.blocks), (0, 0));
    assert!(!end.invalid);
}

#[test]
fn allocation_failure_and_zeroed_failure_do_not_create_live_blocks() {
    let probe = Tracking::new(FailAll);
    let layout = Layout::from_size_align(32, 8).unwrap();
    // SAFETY: valid layouts; null pointers are never dereferenced/deallocated.
    unsafe {
        assert!(probe.alloc(layout).is_null());
        assert!(probe.alloc_zeroed(layout).is_null());
    }
    let end = probe.snapshot();
    assert_eq!((end.alloc, end.zeroed, end.failed), (1, 1, 2));
    assert_eq!((end.live, end.blocks, end.peak), (0, 0, 0));
    assert!(end.validate_after(Snapshot::default()).is_err());
}

#[test]
fn overflow_and_underflow_are_sticky_errors_not_wrapped_success() {
    let probe = Tracking::new(System);
    probe.alloc.store(usize::MAX, SeqCst);
    probe.add(&probe.alloc, 1);
    assert_eq!(probe.alloc.load(SeqCst), usize::MAX);
    assert!(probe.snapshot().invalid);
    let other = Tracking::new(System);
    other.subtract(&other.live, 1);
    assert_eq!(other.live.load(SeqCst), 0);
    assert!(other.snapshot().invalid);
}

#[test]
fn revision_overflow_invalidates_snapshots() {
    let probe = Tracking::new(System);
    probe.revision.store(usize::MAX, SeqCst);
    probe.enter();
    probe.leave();
    assert!(probe.snapshot().invalid);
}

#[test]
fn concurrent_owners_conserve_requested_bytes() {
    let probe = Tracking::new(System);
    std::thread::scope(|scope| {
        for _ in 0..4 {
            let probe = &probe;
            scope.spawn(move || {
                let layout = Layout::from_size_align(48, 8).unwrap();
                for _ in 0..1000 {
                    // SAFETY: one valid allocation/deallocation per iteration.
                    unsafe {
                        let pointer = probe.alloc(layout);
                        assert!(!pointer.is_null());
                        std::ptr::write_bytes(pointer, 42, 48);
                        probe.dealloc(pointer, layout);
                    }
                }
            });
        }
        for _ in 0..100 {
            let sample = probe.snapshot();
            assert!(!sample.invalid);
            assert_eq!(sample.live, sample.blocks * 48);
            assert_eq!(sample.alloc - sample.dealloc, sample.blocks);
        }
    });
    let end = probe.snapshot();
    assert_eq!(
        (end.alloc, end.dealloc, end.live, end.blocks),
        (4000, 4000, 0, 0)
    );
}

#[test]
fn concurrent_live_allocations_preserve_the_peak_after_release() {
    let probe = Tracking::new(System);
    let sizes = [32, 64, 128, 256];
    let held_bytes: usize = sizes.iter().sum();
    let gate = std::sync::Barrier::new(sizes.len() + 1);
    std::thread::scope(|scope| {
        for size in sizes {
            let probe = &probe;
            let gate = &gate;
            scope.spawn(move || {
                let layout = Layout::from_size_align(size, 8).unwrap();
                // SAFETY: the owned allocation remains live until the coordinator
                // samples all workers; it is then released with its exact layout.
                unsafe {
                    let pointer = probe.alloc(layout);
                    assert!(!pointer.is_null());
                    gate.wait();
                    gate.wait();
                    probe.dealloc(pointer, layout);
                }
            });
        }
        gate.wait();
        let held = probe.snapshot();
        assert_eq!(
            (held.live, held.blocks, held.peak),
            (held_bytes, 4, held_bytes)
        );
        gate.wait();
    });
    let released = probe.snapshot();
    assert_eq!(
        (released.live, released.blocks, released.peak),
        (0, 0, held_bytes)
    );
    // A later lower request must not overwrite the concurrent high-water mark.
    let small = Layout::from_size_align(16, 8).unwrap();
    // SAFETY: one successful allocation is released with the same valid layout.
    unsafe {
        let pointer = probe.alloc(small);
        assert!(!pointer.is_null());
        assert_eq!(probe.snapshot().peak, held_bytes);
        probe.dealloc(pointer, small);
    }
    assert_eq!(probe.snapshot().peak, held_bytes);
}

#[test]
fn negative_controls_reject_activity_leaks_and_nonmonotonic_counters() {
    let before = Snapshot::default();
    let held = Snapshot {
        alloc: 1,
        live: 64,
        blocks: 1,
        peak: 64,
        ..before
    };
    assert!(held.no_allocator_calls_since(before).is_err());
    assert!(held.reclaimed_since(before).is_err());
    let released = Snapshot {
        dealloc: 1,
        live: 0,
        blocks: 0,
        ..held
    };
    released.reclaimed_since(before).unwrap();
    assert!(released.no_allocator_calls_since(before).is_err());
    assert!(before.validate_after(released).is_err());
}

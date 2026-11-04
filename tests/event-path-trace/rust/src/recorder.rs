//! Single-core native-callback boundary. Only fixed writes occur with IRQs masked.
use crate::{
    buffer::{Buffer, Event},
    native,
};
use std::{
    alloc::{GlobalAlloc, Layout, System},
    cell::UnsafeCell,
    sync::atomic::{AtomicBool, AtomicI32, Ordering},
};

pub const CAPACITY: usize = 4096;
const ALLOCATIONS: usize = 64;

unsafe extern "C" {
    fn trace_irq_save() -> usize;
    fn trace_irq_restore(flags: usize);
    fn trace_register(callback: extern "C" fn(u32, i32, u32)) -> i32;
    fn trace_instrumented() -> u32;
    fn trace_stack(tid: i32, base: *mut usize, bytes: *mut usize) -> i32;
    fn trace_memory_region(base: usize, bytes: usize) -> u32;
}

struct Static<T>(UnsafeCell<T>);
// The native module admits exactly one CPU. Every access to mutable storage
// uses the target IRQ save/restore barrier; no callback retains a reference.
unsafe impl<T: Send> Sync for Static<T> {}
static EVENTS: Static<Buffer<CAPACITY>> = Static(UnsafeCell::new(Buffer::new()));
static ENABLED: AtomicBool = AtomicBool::new(false);
static OWNER: AtomicI32 = AtomicI32::new(-1);
static RECEIVER: AtomicI32 = AtomicI32::new(-1);
static ALLOCATE: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy)]
pub struct Allocation {
    pub address: usize,
    pub bytes: usize,
    pub alignment: usize,
    pub released: bool,
}
const EMPTY_ALLOC: Allocation = Allocation {
    address: 0,
    bytes: 0,
    alignment: 0,
    released: false,
};
struct AllocationLog {
    items: [Allocation; ALLOCATIONS],
    count: usize,
    overflow: bool,
}
static ALLOCS: Static<AllocationLog> = Static(UnsafeCell::new(AllocationLog {
    items: [EMPTY_ALLOC; ALLOCATIONS],
    count: 0,
    overflow: false,
}));

pub struct TrackingAllocator;
unsafe impl GlobalAlloc for TrackingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { System.alloc(layout) };
        track(pointer, layout);
        pointer
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { System.alloc_zeroed(layout) };
        track(pointer, layout);
        pointer
    }
    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, bytes: usize) -> *mut u8 {
        let next = unsafe { System.realloc(pointer, layout, bytes) };
        if !next.is_null() {
            released(pointer);
            // GlobalAlloc::realloc requires this rounded layout to be valid.
            track(next, unsafe {
                Layout::from_size_align_unchecked(bytes, layout.align())
            });
        }
        next
    }
    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        released(pointer);
        unsafe { System.dealloc(pointer, layout) };
    }
}

fn track(pointer: *mut u8, layout: Layout) {
    if !ALLOCATE.load(Ordering::Relaxed) || pointer.is_null() {
        return;
    }
    let flags = unsafe { trace_irq_save() };
    {
        let log = unsafe { &mut *ALLOCS.0.get() };
        if log.count < ALLOCATIONS {
            log.items[log.count] = Allocation {
                address: pointer as usize,
                bytes: layout.size(),
                alignment: layout.align(),
                released: false,
            };
            log.count += 1;
        } else {
            log.overflow = true;
        }
    }
    unsafe { trace_irq_restore(flags) };
}

fn released(pointer: *mut u8) {
    if !ALLOCATE.load(Ordering::Relaxed) {
        return;
    }
    let flags = unsafe { trace_irq_save() };
    {
        let log = unsafe { &mut *ALLOCS.0.get() };
        for item in &mut log.items[..log.count] {
            if item.address == pointer as usize {
                item.released = true;
            }
        }
    }
    unsafe { trace_irq_restore(flags) };
}

pub fn constructor<T>(create: impl FnOnce() -> T) -> T {
    // This scope precedes worker creation and any trace window.
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            ALLOCATE.store(false, Ordering::Relaxed);
        }
    }
    ALLOCATE.store(true, Ordering::Relaxed);
    let _reset = Reset;
    create()
}

pub fn allocation_snapshot() -> native::Result<Vec<Allocation>> {
    // Capture is already disabled; callbacks never touch allocation storage.
    let log = unsafe { &*ALLOCS.0.get() };
    if log.overflow {
        return Err("allocation-log-overflow");
    }
    Ok(log.items[..log.count].to_vec())
}

pub fn register() -> native::Result<bool> {
    if unsafe { trace_register(sdk_note) } != 0 {
        return Err("note-driver-register");
    }
    Ok(unsafe { trace_instrumented() } == 1)
}

pub fn enable(owner: i32, receiver: i32) {
    OWNER.store(owner, Ordering::Relaxed);
    RECEIVER.store(receiver, Ordering::Relaxed);
    ENABLED.store(true, Ordering::Relaxed);
}

pub fn disable() {
    let flags = unsafe { trace_irq_save() };
    ENABLED.store(false, Ordering::Relaxed);
    unsafe { trace_irq_restore(flags) };
}

// Required native SDK callback ABI, never used for Rust-to-Rust calls.
extern "C" fn sdk_note(kind: u32, tid: i32, detail: u32) {
    record(kind, tid, detail);
}

pub fn record(kind: u32, tid: i32, detail: u32) {
    // Crucially, disabled callbacks do not borrow the buffer being read below.
    if !ENABLED.load(Ordering::Relaxed) {
        return;
    }
    if kind != 4
        && kind != 5
        && tid != OWNER.load(Ordering::Relaxed)
        && tid != RECEIVER.load(Ordering::Relaxed)
    {
        return;
    }
    let flags = unsafe { trace_irq_save() };
    {
        let events = unsafe { &mut *EVENTS.0.get() };
        events.push(Event {
            ticks: native::counter(),
            kind,
            tid,
            detail,
        });
    }
    // Every mutable reference ends before restoring IRQs / possible reentry.
    unsafe { trace_irq_restore(flags) };
}

pub fn snapshot() -> native::Result<Vec<Event>> {
    if ENABLED.load(Ordering::Relaxed) {
        return Err("snapshot-while-enabled");
    }
    let buffer = unsafe { &*EVENTS.0.get() };
    if buffer.dropped != 0 {
        return Err("trace-overflow");
    }
    Ok(buffer.events[..buffer.len].to_vec())
}

pub fn stack(tid: i32) -> native::Result<(usize, usize)> {
    let (mut base, mut bytes) = (0, 0);
    if unsafe { trace_stack(tid, &mut base, &mut bytes) } != 0 || bytes == 0 {
        return Err("stack-snapshot");
    }
    Ok((base, bytes))
}

pub fn region(address: usize, bytes: usize) -> u32 {
    unsafe { trace_memory_region(address, bytes) }
}

pub fn buffer_location() -> (usize, usize) {
    (
        EVENTS.0.get() as usize,
        std::mem::size_of::<Buffer<CAPACITY>>(),
    )
}

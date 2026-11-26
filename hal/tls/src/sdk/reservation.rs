//! Heap-independent reconnect storage. Each connection owns one exclusive arena.
use super::{try_boxed, Error};
use std::{
    alloc::{alloc, alloc_zeroed, dealloc, Layout},
    cell::{Cell, RefCell},
    marker::PhantomData,
    ops::{Deref, DerefMut},
    ptr::{self, NonNull},
};

const ALIGN: usize = 16;
const BYTES: usize = 24 * 1024;
const SPANS: usize = 96;
const SLOTS: usize = 2;
const LAYOUT: Layout = match Layout::from_size_align(BYTES, ALIGN) {
    Ok(layout) => layout,
    Err(_) => panic!("invalid TLS arena layout"),
};

#[derive(Clone, Copy)]
struct Span {
    start: u16,
    units: u16,
}

struct Arena {
    bytes: NonNull<u8>,
    leased: Cell<bool>,
    used: Cell<usize>,
    spans: RefCell<[Span; SPANS]>,
}

std::thread_local! {
    // The SDK lease admits one identity, which owns the entire registry.
    // A scope selects one arena while calls execute on the owning thread.
    static ARENAS: Cell<[*const Arena; SLOTS]> = const { Cell::new([ptr::null(); SLOTS]) };
    static ACTIVE: Cell<*const Arena> = const { Cell::new(ptr::null()) };
}

impl Arena {
    fn new() -> Result<Box<Self>, Error> {
        // SAFETY: a nonempty layout at the native allocator's maximum alignment.
        let bytes = NonNull::new(unsafe { alloc_zeroed(LAYOUT) }).ok_or(Error::Capacity)?;
        try_boxed(Self {
            bytes,
            leased: Cell::new(false),
            used: Cell::new(0),
            spans: RefCell::new([Span { start: 0, units: 0 }; SPANS]),
        })
    }

    fn allocate(&self, layout: Layout, clear: bool) -> Option<NonNull<u8>> {
        if layout.align() > ALIGN || layout.size() > BYTES {
            return None;
        }
        let units = layout.size().max(1).div_ceil(ALIGN);
        let count = self.used.get();
        if count == SPANS {
            return None;
        }
        let mut spans = self.spans.borrow_mut();
        let mut start = 0;
        let index = spans[..count]
            .iter()
            .position(|span| {
                if start + units <= usize::from(span.start) {
                    true
                } else {
                    start = usize::from(span.start) + usize::from(span.units);
                    false
                }
            })
            .unwrap_or(count);
        if (start + units) * ALIGN > BYTES {
            return None;
        }
        spans.copy_within(index..count, index + 1);
        spans[index] = Span {
            start: start as u16,
            units: units as u16,
        };
        self.used.set(count + 1);
        // SAFETY: allocated backing storage, in bounds and disjoint from the
        // other live spans. Every span starts at an ALIGN-byte boundary.
        let bytes = unsafe { self.bytes.add(start * ALIGN) };
        if clear {
            unsafe { bytes.as_ptr().write_bytes(0, layout.size()) };
        }
        Some(bytes)
    }

    fn release(&self, address: usize) -> bool {
        let base = self.bytes.as_ptr() as usize;
        if !(base..base + BYTES).contains(&address) {
            return false;
        }
        let mut spans = self.spans.borrow_mut();
        let count = self.used.get();
        let index = spans[..count]
            .iter()
            .position(|span| address == base + usize::from(span.start) * ALIGN)
            .expect("TLS freed an unowned arena span");
        let length = usize::from(spans[index].units) * ALIGN;
        // Wipe credentials and plaintext before another connection can reuse
        // this span. Only the retiring allocation can still reference its bytes.
        unsafe {
            super::ffi::mbedtls_platform_zeroize(
                self.bytes.as_ptr().add(address - base).cast(),
                length,
            );
        }
        spans.copy_within(index + 1..count, index);
        self.used.set(count - 1);
        true
    }
}

impl Drop for Arena {
    fn drop(&mut self) {
        // SAFETY: the identity outlives all connections and owns this allocation.
        unsafe { dealloc(self.bytes.as_ptr(), LAYOUT) };
    }
}

#[derive(Default)]
pub(super) struct Reservations([Option<Box<Arena>>; SLOTS]);

impl Reservations {
    pub(super) fn prepare(&mut self, count: usize) -> Result<(), Error> {
        if !(1..=SLOTS).contains(&count) || self.0[0].is_some() {
            return Err(Error::InvalidInput);
        }
        let mut arenas = Self::default();
        for slot in &mut arenas.0[..count] {
            *slot = Some(Arena::new()?);
        }
        ARENAS.set(
            arenas
                .0
                .each_ref()
                .map(|arena| arena.as_deref().map_or(ptr::null(), ptr::from_ref)),
        );
        // Swap the new allocation in. The original value was empty.
        self.0 = std::mem::take(&mut arenas.0);
        Ok(())
    }

    pub(super) fn checkout(&self) -> Result<Slot<'_>, Error> {
        if self.0[0].is_none() {
            return Ok(Slot::default());
        }
        let arena = self
            .0
            .iter()
            .flatten()
            .find(|arena| !arena.leased.get())
            .ok_or(Error::Capacity)?;
        arena.leased.set(true);
        Ok(Slot(Some(arena)))
    }
}

impl Drop for Reservations {
    fn drop(&mut self) {
        if self.0[0].is_some() {
            ARENAS.set([ptr::null(); SLOTS]);
        }
    }
}

#[derive(Default)]
pub(super) struct Slot<'a>(Option<&'a Arena>);

impl Slot<'_> {
    pub(super) fn enter(&self) -> Scope<'_> {
        let prior = ACTIVE.replace(self.0.map_or(ptr::null(), ptr::from_ref));
        Scope(prior, PhantomData)
    }
}

impl Drop for Slot<'_> {
    fn drop(&mut self) {
        if let Some(arena) = self.0 {
            // Reuse requires every Rust and native allocation to have retired.
            // A provider leak quarantines this slot instead of recycling bytes.
            arena.leased.set(arena.used.get() != 0);
        }
    }
}

pub(super) struct Scope<'a>(*const Arena, PhantomData<&'a Arena>);

impl Drop for Scope<'_> {
    fn drop(&mut self) {
        ACTIVE.set(self.0);
    }
}

pub(super) fn allocate_reserved(layout: Layout, zeroed: bool) -> Option<Option<NonNull<u8>>> {
    ACTIVE.with(|arena| {
        // SAFETY: ACTIVE is entered only with a borrowed slot and restored by
        // its scope. Calls occur synchronously on the identity owner thread.
        unsafe { arena.get().as_ref() }.map(|arena| arena.allocate(layout, zeroed))
    })
}

pub(super) fn release_reserved(pointer: *mut u8) -> bool {
    ARENAS.with(|arenas| {
        arenas.get().into_iter().any(|arena| {
            // SAFETY: registered arenas remain owned until the identity retires.
            unsafe { arena.as_ref() }.is_some_and(|arena| arena.release(pointer as usize))
        })
    })
}

pub(super) fn allocate(layout: Layout, zeroed: bool) -> Result<NonNull<u8>, Error> {
    if let Some(result) = allocate_reserved(layout, zeroed) {
        return result.ok_or(Error::Capacity);
    }
    // SAFETY: a nonempty, validated layout; released through the same allocator.
    let pointer = unsafe {
        if zeroed {
            alloc_zeroed(layout)
        } else {
            alloc(layout)
        }
    };
    NonNull::new(pointer).ok_or(Error::Capacity)
}

pub(super) unsafe fn release(pointer: NonNull<u8>, layout: Layout) {
    if !release_reserved(pointer.as_ptr()) {
        // SAFETY: caller owns this live allocation made with this layout.
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

/// Owns one typed connection value at a stable address for the BIO callback.
pub(super) struct Owned<T>(NonNull<T>);

impl<T> Owned<T> {
    pub(super) fn new(value: T) -> Result<Self, Error> {
        let layout = Layout::new::<T>();
        let pointer = if layout.size() == 0 {
            NonNull::dangling()
        } else {
            allocate(layout, false)?.cast::<T>()
        };
        // SAFETY: the pointer is exclusively owned and properly aligned for T.
        unsafe { pointer.as_ptr().write(value) };
        Ok(Self(pointer))
    }
}

impl<T> Deref for Owned<T> {
    type Target = T;
    fn deref(&self) -> &T {
        // SAFETY: initialized during construction and retained for this borrow.
        unsafe { self.0.as_ref() }
    }
}

impl<T> DerefMut for Owned<T> {
    fn deref_mut(&mut self) -> &mut T {
        // SAFETY: this owner has exclusive access during the borrow.
        unsafe { self.0.as_mut() }
    }
}

impl<T> Drop for Owned<T> {
    fn drop(&mut self) {
        // SAFETY: drop the value before returning its storage to its allocator.
        unsafe {
            self.0.as_ptr().drop_in_place();
            let layout = Layout::new::<T>();
            if layout.size() != 0 {
                release(self.0.cast(), layout);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simultaneous_leases_isolate_storage_and_reuse_it_after_teardown() {
        let mut reservations = Reservations::default();
        reservations.prepare(2).unwrap();
        let first = reservations.checkout().unwrap();
        let second = reservations.checkout().unwrap();
        assert!(matches!(reservations.checkout(), Err(Error::Capacity)));

        let a = {
            let _scope = first.enter();
            let allocation = allocate(LAYOUT, true).unwrap();
            assert!(matches!(
                allocate(Layout::new::<u8>(), false),
                Err(Error::Capacity)
            ));
            allocation
        };
        let b = {
            let _scope = second.enter();
            allocate(LAYOUT, false).unwrap()
        };
        assert_ne!(a, b);
        // Retire native and Rust objects after leaving their call scopes.
        assert!(release_reserved(a.as_ptr()));
        drop(first);
        let replacement = reservations.checkout().unwrap();
        {
            let _scope = replacement.enter();
            assert_eq!(allocate(LAYOUT, false).unwrap(), a);
        }
        assert!(release_reserved(a.as_ptr()));
        assert!(release_reserved(b.as_ptr()));
    }
}

//! Temporary allocations from the pinned SDK's established TLS record path.
use super::{ffi, Error};
use std::{
    alloc::{alloc_zeroed, dealloc, Layout},
    cell::{Cell, UnsafeCell},
    ffi::c_void,
    ptr::{self, NonNull},
};

// PSA copies the nonce, associated data, input and output. Record slots
// include the pinned 4 KiB TLS content limit and room for GCM record overhead.
const SMALL: usize = 32;
const LARGE: usize = 4096 + 64;
const SLOTS: [(usize, usize); 4] = [
    (0, SMALL),
    (SMALL, SMALL),
    (2 * SMALL, LARGE),
    (2 * SMALL + LARGE, LARGE),
];
const BYTES: usize = 2 * (SMALL + LARGE);

#[derive(Clone, Copy)]
#[repr(u8)]
enum Fault {
    None = 0,
    Capacity,
    Allocator,
}

#[repr(C, align(16))]
struct Workspace {
    bytes: UnsafeCell<[u8; BYTES]>,
    used: Cell<u8>,
    active: Cell<bool>,
    fault: Cell<Fault>,
}

thread_local! {
    // An address, with no thread-local destructor. The identity owns the
    // backing memory even when the identity itself lives in thread-local storage.
    static WORKSPACE: Cell<*const Workspace> = const { Cell::new(ptr::null()) };
}

pub(super) struct Memory(NonNull<Workspace>);
impl Memory {
    pub(super) fn prepare() -> Result<Self, Error> {
        // SAFETY: every field of Workspace has a valid all-zero representation.
        let pointer = NonNull::new(unsafe { alloc_zeroed(Layout::new::<Workspace>()) }.cast())
            .ok_or(Error::Capacity)?;
        WORKSPACE.with(|slot| slot.set(pointer.as_ptr()));
        Ok(Self(pointer))
    }

    pub(super) fn enter(&self) -> Result<Scope<'_>, Error> {
        // SAFETY: the live owner keeps this workspace allocated.
        let workspace = unsafe { self.0.as_ref() };
        if workspace.active.get() {
            return Err(Error::Busy);
        }
        if workspace.used.get() != 0 {
            return Err(Error::Crypto);
        }
        workspace.active.set(true);
        workspace.fault.set(Fault::None);
        Ok(Scope(workspace))
    }
}
impl Drop for Memory {
    fn drop(&mut self) {
        WORKSPACE.with(|slot| slot.set(ptr::null()));
        // SAFETY: the SDK owner retires its native state before this storage.
        unsafe {
            let pointer = self.0.as_ptr().cast();
            ffi::mbedtls_platform_zeroize(pointer, size_of::<Workspace>());
            dealloc(pointer.cast(), Layout::new::<Workspace>());
        }
    }
}

fn workspace<T>(use_workspace: impl FnOnce(Option<&Workspace>) -> T) -> T {
    WORKSPACE.with(|slot| {
        // SAFETY: Memory registers the allocation on its owning thread and
        // deregisters it after all synchronous SDK calls and teardown return.
        use_workspace(unsafe { slot.get().as_ref() })
    })
}

pub(super) struct Scope<'a>(&'a Workspace);
impl Scope<'_> {
    pub(super) fn finish(self) -> Result<(), Error> {
        if self.0.used.get() != 0 {
            return Err(Error::Crypto);
        }
        match self.0.fault.get() {
            Fault::None => Ok(()),
            Fault::Capacity => Err(Error::Capacity),
            Fault::Allocator => Err(Error::Crypto),
        }
    }
}
impl Drop for Scope<'_> {
    fn drop(&mut self) {
        self.0.active.set(false);
    }
}

/// Mbed TLS allocation callback. Only established record I/O uses the slots.
#[no_mangle]
extern "C" fn tls_record_calloc(count: usize, size: usize) -> *mut c_void {
    let record = workspace(|workspace| {
        let workspace = workspace.filter(|workspace| workspace.active.get())?;
        if let Some(len) = count.checked_mul(size) {
            for (i, (offset, capacity)) in SLOTS.into_iter().enumerate() {
                let bit = 1 << i;
                if len <= capacity && workspace.used.get() & bit == 0 {
                    workspace.used.set(workspace.used.get() | bit);
                    // SAFETY: this free slot is within the live allocation,
                    // and the SDK has exclusive use until its paired free.
                    let ptr = unsafe { workspace.bytes.get().cast::<u8>().add(offset) };
                    unsafe { ptr.write_bytes(0, len) };
                    return Some(ptr.cast());
                }
            }
        }
        workspace.fault.set(Fault::Capacity);
        Some(ptr::null_mut())
    });
    // SAFETY: outside record I/O the SDK retains its ordinary libc allocator.
    record.unwrap_or_else(|| unsafe { ffi::calloc(count, size) })
}

/// Mbed TLS free callback. The SDK supplies a pointer from its paired allocator.
#[no_mangle]
unsafe extern "C" fn tls_record_free(pointer: *mut c_void) {
    if pointer.is_null() {
        return;
    }
    let released = workspace(|workspace| {
        let Some(workspace) = workspace else {
            return false;
        };
        for (i, (offset, _)) in SLOTS.into_iter().enumerate() {
            if pointer.cast() == workspace.bytes.get().cast::<u8>().wrapping_add(offset) {
                let bit = 1 << i;
                if workspace.used.get() & bit == 0 {
                    workspace.fault.set(Fault::Allocator);
                }
                workspace.used.set(workspace.used.get() & !bit);
                return true;
            }
        }
        if workspace.active.get() {
            // Retire the object with its original allocator. A release from
            // the shared heap here violates the pinned record-I/O contract.
            workspace.fault.set(Fault::Allocator);
        }
        false
    });
    if !released {
        // SAFETY: a non-slot pointer is an SDK allocation from libc calloc.
        unsafe { ffi::free(pointer) };
    }
}

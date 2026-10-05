//! Exclusive, statically stored service state; diagnostics do not live on stacks.
use core::{
    cell::UnsafeCell,
    marker::PhantomData,
    ops::{Deref, DerefMut},
    sync::atomic::{AtomicBool, Ordering},
};
pub struct Slot<T> {
    claimed: AtomicBool,
    value: UnsafeCell<T>,
}
// SAFETY: access is available only through an exclusive lease. Acquire/Release
// synchronizes successive owners, including host tests on different threads.
unsafe impl<T: Send> Sync for Slot<T> {}
impl<T> Slot<T> {
    pub const fn new(value: T) -> Self {
        Self {
            claimed: AtomicBool::new(false),
            value: UnsafeCell::new(value),
        }
    }
    pub fn claim(&self) -> Option<Lease<'_, T>> {
        self.claimed
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .ok()
            .map(|_| Lease {
                slot: self,
                exclusive: PhantomData,
            })
    }
}
pub struct Lease<'a, T> {
    slot: &'a Slot<T>,
    exclusive: PhantomData<&'a mut T>,
}
impl<T> Deref for Lease<'_, T> {
    type Target = T;
    fn deref(&self) -> &T {
        // SAFETY: this lease is the only live owner; references cannot outlive it.
        unsafe { &*self.slot.value.get() }
    }
}
impl<T> DerefMut for Lease<'_, T> {
    fn deref_mut(&mut self) -> &mut T {
        // SAFETY: exclusive &mut lease prevents aliasing within the owner.
        unsafe { &mut *self.slot.value.get() }
    }
}
impl<T> Drop for Lease<'_, T> {
    fn drop(&mut self) {
        self.slot.claimed.store(false, Ordering::Release);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn leases_are_exclusive_and_reusable() {
        let slot = Slot::new(0u32);
        let mut owner = slot.claim().unwrap();
        assert!(slot.claim().is_none());
        *owner = 42;
        drop(owner);
        assert_eq!(*slot.claim().unwrap(), 42);
    }
    #[test]
    fn leases_synchronize_threads() {
        let slot = std::sync::Arc::new(Slot::new(0u32));
        let other = slot.clone();
        std::thread::spawn(move || {
            *other.claim().unwrap() = 73;
        })
        .join()
        .unwrap();
        assert_eq!(*slot.claim().unwrap(), 73);
    }
}

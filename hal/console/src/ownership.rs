//! Restore the resource, close it, then release acquisition; uncertainty sticks.
#![forbid(unsafe_code)]
use core::sync::atomic::{AtomicU8, Ordering};
use std::io;

pub(crate) struct Lease<'a> {
    state: &'a AtomicU8,
    uncertain: bool,
}
impl<'a> Lease<'a> {
    pub(crate) fn acquire(state: &'a AtomicU8) -> io::Result<Self> {
        state
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|value| {
                if value == 1 {
                    io::ErrorKind::WouldBlock.into()
                } else {
                    io::Error::other("console state uncertain")
                }
            })?;
        Ok(Self {
            state,
            uncertain: false,
        })
    }
    pub(crate) fn mark_uncertain(&mut self) {
        self.uncertain = true;
    }
    pub(crate) fn restored(&mut self) {
        self.uncertain = false;
    }
}
impl Drop for Lease<'_> {
    fn drop(&mut self) {
        self.state
            .store(if self.uncertain { 2 } else { 0 }, Ordering::Release);
    }
}
pub(crate) trait Restore {
    fn restore(&mut self) -> io::Result<()>;
}
pub(crate) struct Owner<'a, R: Restore> {
    // Field drop order keeps the lease until after the resource has closed.
    pub(crate) resource: R,
    lease: Lease<'a>,
}
impl<'a, R: Restore> Owner<'a, R> {
    pub(crate) fn new(resource: R, lease: Lease<'a>) -> Self {
        Self { resource, lease }
    }
}
impl<R: Restore> Drop for Owner<'_, R> {
    fn drop(&mut self) {
        self.lease.mark_uncertain();
        if self.resource.restore().is_ok() {
            self.lease.restored();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};
    struct Resource<'a> {
        state: &'a AtomicU8,
        events: Rc<RefCell<std::vec::Vec<&'static str>>>,
        fail: bool,
    }
    impl Restore for Resource<'_> {
        fn restore(&mut self) -> io::Result<()> {
            self.events.borrow_mut().push("restore");
            if self.fail {
                Err(io::Error::other("restore witness"))
            } else {
                Ok(())
            }
        }
    }
    impl Drop for Resource<'_> {
        fn drop(&mut self) {
            assert!(
                Lease::acquire(self.state).is_err(),
                "lease must survive descriptor closure"
            );
            self.events.borrow_mut().push("close");
        }
    }
    #[test]
    fn restores_and_closes_before_reacquisition_on_drop_and_unwind() {
        for unwind in [false, true] {
            let state = AtomicU8::new(0);
            let events = Rc::new(RefCell::new(std::vec::Vec::new()));
            let owner = Owner::new(
                Resource {
                    state: &state,
                    events: events.clone(),
                    fail: false,
                },
                Lease::acquire(&state).unwrap(),
            );
            assert!(Lease::acquire(&state).is_err());
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let _owner = owner;
                assert!(!unwind, "owner unwind witness");
            }));
            assert_eq!(result.is_err(), unwind);
            assert_eq!(&*events.borrow(), &["restore", "close"]);
            assert!(Lease::acquire(&state).is_ok());
        }
    }
    #[test]
    fn uncertain_setup_or_restore_quarantines_future_owners() {
        for restore in [false, true] {
            let state = AtomicU8::new(0);
            let events = Rc::new(RefCell::new(std::vec::Vec::new()));
            let mut lease = Lease::acquire(&state).unwrap();
            if restore {
                drop(Owner::new(
                    Resource {
                        state: &state,
                        events,
                        fail: true,
                    },
                    lease,
                ));
            } else {
                lease.mark_uncertain();
                drop(lease);
            }
            assert!(Lease::acquire(&state).is_err());
        }
    }
}

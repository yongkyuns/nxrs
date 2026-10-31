//! Quiet-interface ownership is independent of the normal session worker.
use crate::{Error, QuiescenceControl};

pub(crate) trait Interface {
    /// Success includes native completion and a matching state observation.
    fn set_enabled(&mut self, enabled: bool) -> Result<(), Error>;
}

#[derive(Clone, Copy)]
enum State {
    Active,
    Quiet,
    Fault(Error),
}

pub(crate) struct Owner<D, L> {
    driver: D,
    state: State,
    // Close control resources before releasing admission.
    lease: Option<L>,
}

impl<D, L> Owner<D, L> {
    pub(crate) fn new(driver: D, lease: L) -> Self {
        Self {
            driver,
            state: State::Active,
            lease: Some(lease),
        }
    }
}

impl<D: Interface, L> Owner<D, L> {
    fn transition(&mut self, enabled: bool) -> Result<(), Error> {
        match self.state {
            State::Fault(error) => return Err(error),
            State::Active if enabled => return Ok(()),
            State::Quiet if !enabled => return Ok(()),
            _ => (),
        }
        match self.driver.set_enabled(enabled) {
            Ok(()) => {
                self.state = if enabled { State::Active } else { State::Quiet };
                Ok(())
            }
            Err(error) => {
                self.state = State::Fault(error);
                Err(error)
            }
        }
    }
}

impl<D: Interface, L> QuiescenceControl for Owner<D, L> {
    fn quiesce(&mut self) -> Result<(), Error> {
        self.transition(false)
    }
    fn resume(&mut self) -> Result<(), Error> {
        self.transition(true)
    }
}

impl<D, L> Drop for Owner<D, L> {
    fn drop(&mut self) {
        if matches!(self.state, State::Fault(_)) {
            // Native state is uncertain. No hidden blocking cleanup in Drop;
            // prevent any new session from being admitted until reset.
            if let Some(lease) = self.lease.take() {
                std::mem::forget(lease);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    struct Lease<'a>(&'a Cell<bool>, &'a Cell<bool>);
    impl Drop for Lease<'_> {
        fn drop(&mut self) {
            assert!(
                self.1.get(),
                "control resources must close before admission"
            );
            self.0.set(false);
        }
    }
    struct Driver<'a> {
        held: &'a Cell<bool>,
        calls: &'a Cell<usize>,
        closed: &'a Cell<bool>,
        fail_on: usize,
    }
    impl Interface for Driver<'_> {
        fn set_enabled(&mut self, _: bool) -> Result<(), Error> {
            let call = self.calls.get() + 1;
            self.calls.set(call);
            if call == self.fail_on {
                Err(Error::Io)
            } else {
                Ok(())
            }
        }
    }
    impl Drop for Driver<'_> {
        fn drop(&mut self) {
            assert!(self.held.get(), "admission must outlive control resources");
            self.closed.set(true);
        }
    }

    #[test]
    fn quiet_owner_retains_admission_and_quarantines_uncertain_transitions() {
        // Read-only acquisition, quiet Drop, successful resume, down failure
        // after possible side effects, and partial resume failure.
        for (operations, fail_on) in [(0, 0), (1, 0), (2, 0), (1, 1), (2, 2)] {
            let (held, closed, calls) = (Cell::new(true), Cell::new(false), Cell::new(0));
            let mut owner = Owner::new(
                Driver {
                    held: &held,
                    calls: &calls,
                    closed: &closed,
                    fail_on,
                },
                Lease(&held, &closed),
            );
            assert_eq!(owner.resume(), Ok(()));
            if operations > 0 {
                let expected = if fail_on == 1 { Err(Error::Io) } else { Ok(()) };
                assert_eq!(owner.quiesce(), expected);
                assert_eq!(owner.quiesce(), expected);
            }
            if operations > 1 {
                let expected = if fail_on == 2 { Err(Error::Io) } else { Ok(()) };
                assert_eq!(owner.resume(), expected);
                assert_eq!(owner.resume(), expected);
            }
            assert!(held.get() && !closed.get());
            if fail_on != 0 {
                assert_eq!(owner.quiesce(), Err(Error::Io));
                assert_eq!(owner.resume(), Err(Error::Io));
            }
            assert_eq!(calls.get(), operations);
            drop(owner);
            assert!(closed.get());
            assert_eq!(held.get(), fail_on != 0);
        }
    }
}

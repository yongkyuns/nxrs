//! Shared admission and sticky failure handling; no platform selection or I/O.
use crate::staging::{self, Slot};
use crate::{Control, DeviceError, State};

pub(crate) trait Backend {
    fn reboot(&mut self) -> Result<core::convert::Infallible, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    type Slot: Slot;
    fn secondary(&mut self) -> Result<Self::Slot, DeviceError>;
    fn state(&mut self) -> Result<State, DeviceError>;
    fn request_trial(&mut self) -> Result<(), DeviceError>;
    fn confirm(&mut self) -> Result<(), DeviceError>;
}

pub(crate) struct Owner<B> {
    backend: B,
    fault: Option<DeviceError>,
}

impl<B: Backend> Owner<B> {
    pub(crate) fn new(backend: B) -> Result<Self, DeviceError> {
        let mut owner = Self {
            backend,
            fault: None,
        };
        owner.state()?;
        Ok(owner)
    }

    fn transition(
        &mut self,
        from: State,
        to: State,
        operation: fn(&mut B) -> Result<(), DeviceError>,
    ) -> Result<(), DeviceError> {
        if self.state()? != from {
            return Err(DeviceError::Busy);
        }
        if let Err(error) = operation(&mut self.backend) {
            self.fault = Some(error);
            return Err(error);
        }
        if self.state()? != to {
            self.fault = Some(DeviceError::InvalidState);
            return Err(DeviceError::InvalidState);
        }
        Ok(())
    }
}

impl<B: Backend> Control for Owner<B> {
    fn reboot(&mut self) -> Result<core::convert::Infallible, DeviceError> {
        if self.state()? != State::Pending {
            return Err(DeviceError::Busy);
        }
        self.backend.reboot()
    }
    fn stage_update(
        &mut self,
        source: &mut dyn std::io::Read,
        length: u32,
    ) -> Result<(), DeviceError> {
        if self.state()? != State::Stable {
            return Err(DeviceError::Busy);
        }
        // Also retain failure if the caller's reader unwinds mid-operation.
        self.fault = Some(DeviceError::Io);
        let result = self
            .backend
            .secondary()
            .and_then(|mut slot| staging::copy(&mut slot, source, length));
        self.fault = result.err();
        result?;
        if self.state()? != State::Stable {
            self.fault = Some(DeviceError::InvalidState);
            return Err(DeviceError::InvalidState);
        }
        Ok(())
    }
    fn state(&mut self) -> Result<State, DeviceError> {
        if let Some(error) = self.fault {
            return Err(error);
        }
        let result = self.backend.state();
        if let Err(error) = result {
            self.fault = Some(error);
        }
        result
    }

    fn request_trial(&mut self) -> Result<(), DeviceError> {
        self.transition(State::Stable, State::Pending, B::request_trial)
    }

    fn confirm(&mut self) -> Result<(), DeviceError> {
        self.transition(State::Trial, State::Stable, B::confirm)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reset_is_admitted_only_after_pending_readback() {
        let mut owner = Owner::new(Fake {
            state: State::Stable,
            failure: None,
            writes: 0,
            ignore: false,
        })
        .unwrap();
        assert_eq!(owner.reboot(), Err(DeviceError::Busy));
        owner.stage_update(&mut &[0; 32][..], 32).unwrap();
        owner.request_trial().unwrap();
        assert_eq!(owner.reboot(), Err(DeviceError::Unsupported));
    }
    struct Fake {
        state: State,
        failure: Option<DeviceError>,
        writes: u8,
        ignore: bool,
    }
    impl Backend for Fake {
        type Slot = crate::staging::tests::Memory;
        fn secondary(&mut self) -> Result<Self::Slot, DeviceError> {
            self.writes += 1;
            if let Some(error) = self.failure {
                return Err(error);
            }
            Ok(Self::Slot::new())
        }
        fn state(&mut self) -> Result<State, DeviceError> {
            Ok(self.state)
        }
        fn request_trial(&mut self) -> Result<(), DeviceError> {
            self.writes += 1;
            if let Some(e) = self.failure {
                return Err(e);
            }
            if !self.ignore {
                self.state = State::Pending;
            }
            Ok(())
        }
        fn confirm(&mut self) -> Result<(), DeviceError> {
            self.writes += 1;
            self.state = State::Stable;
            Ok(())
        }
    }
    fn owner(state: State) -> Owner<Fake> {
        Owner::new(Fake {
            state,
            failure: None,
            writes: 0,
            ignore: false,
        })
        .unwrap()
    }
    #[test]
    fn trial_request_and_health_confirmation_have_separate_admission() {
        let mut control = owner(State::Stable);
        assert_eq!(control.confirm(), Err(DeviceError::Busy));
        assert_eq!(control.backend.writes, 0);
        control.request_trial().unwrap();
        assert_eq!(control.state(), Ok(State::Pending));
        assert_eq!(control.request_trial(), Err(DeviceError::Busy));
        assert_eq!(control.confirm(), Err(DeviceError::Busy));
        assert_eq!(control.backend.writes, 1);
        let mut control = owner(State::Trial);
        assert_eq!(control.request_trial(), Err(DeviceError::Busy));
        control.confirm().unwrap();
        assert_eq!(control.state(), Ok(State::Stable));
    }
    #[test]
    fn failed_or_unobserved_writes_are_sticky() {
        for failure in [Some(DeviceError::Io), None] {
            let mut control = owner(State::Stable);
            control.backend.failure = failure;
            control.backend.ignore = true;
            let expected = failure.unwrap_or(DeviceError::InvalidState);
            assert_eq!(control.request_trial(), Err(expected));
            assert_eq!(control.state(), Err(expected));
            assert_eq!(control.request_trial(), Err(expected));
            assert_eq!(control.confirm(), Err(expected));
            assert_eq!(control.backend.writes, 1);
        }
    }
    #[test]
    fn staging_admission_failure_and_reader_unwind_are_sticky() {
        let mut control = owner(State::Stable);
        control.stage_update(&mut &[0; 32][..], 32).unwrap();
        assert_eq!(control.state(), Ok(State::Stable));
        assert_eq!(control.backend.writes, 1);
        control.request_trial().unwrap();
        assert_eq!(control.state(), Ok(State::Pending));
        for state in [State::Pending, State::Trial] {
            let mut control = owner(state);
            assert_eq!(
                control.stage_update(&mut &b"image"[..], 5),
                Err(DeviceError::Busy)
            );
            assert_eq!(control.backend.writes, 0);
        }
        let mut control = owner(State::Stable);
        control.backend.failure = Some(DeviceError::Io);
        assert_eq!(
            control.stage_update(&mut &b"image"[..], 5),
            Err(DeviceError::Io)
        );
        assert_eq!(control.request_trial(), Err(DeviceError::Io));
        assert_eq!(control.backend.writes, 1);
        struct Panics;
        impl std::io::Read for Panics {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                panic!("interrupted source")
            }
        }
        let mut control = owner(State::Stable);
        assert!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                || control.stage_update(&mut Panics, 32)
            ))
            .is_err()
        );
        assert_eq!(control.request_trial(), Err(DeviceError::Io));
    }
}

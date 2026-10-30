//! Shared admission and sticky failure handling; no platform selection or I/O.
use crate::{Control, DeviceError, State};

pub(crate) trait Backend {
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
    struct Fake {
        state: State,
        failure: Option<DeviceError>,
        writes: u8,
        ignore: bool,
    }
    impl Backend for Fake {
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
}

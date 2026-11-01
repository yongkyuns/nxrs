//! Bounded RAM delivery precedes joined retirement and inactive-slot writes.
use std::sync::{
    atomic::{AtomicBool, AtomicU8, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

#[derive(Clone)]
pub struct Update(Arc<Shared>);
struct Shared {
    image: Mutex<Option<Vec<u8>>>,
    ready: AtomicBool,
    admission: AtomicU8,
    pub maximum: u32,
    pub timeout: Duration,
}

impl Update {
    pub fn new(maximum: u32, timeout: Duration) -> Result<Self, crate::Error> {
        if maximum < 32 || timeout.is_zero() || timeout > Duration::from_secs(300) {
            return Err(crate::Error::Tls(tls::Error::InvalidInput));
        }
        Ok(Self(Arc::new(Shared {
            image: Mutex::new(None),
            ready: AtomicBool::new(false),
            admission: AtomicU8::new(0),
            maximum,
            timeout,
        })))
    }
    /// Capture never waits on a lower-priority receiver's mailbox lock.
    pub fn ready(&self) -> bool {
        self.0.ready.load(Ordering::Acquire)
    }
    /// Atomically exclude new uploads before retiring for a media reconnect.
    /// An admitted upload retains its existing fixed deadline instead.
    pub fn retire_if_idle(&self) -> bool {
        self.0
            .admission
            .compare_exchange(0, 2, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
    /// A new camera session gets fresh admission only after every prior
    /// receiver has joined. Never discard a completed firmware upload.
    pub fn renew(self) -> Result<Self, crate::Error> {
        let shared = Arc::try_unwrap(self.0).map_err(|_| crate::Error::Tls(tls::Error::Busy))?;
        if shared.ready.load(Ordering::Acquire) {
            return Err(crate::Error::Tls(tls::Error::InvalidInput));
        }
        Self::new(shared.maximum, shared.timeout)
    }
    pub(crate) fn begin(&self) -> Option<Receiving<'_>> {
        self.0
            .admission
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .ok()?;
        Some(Receiving(self))
    }
    pub(crate) fn maximum(&self) -> u32 {
        self.0.maximum
    }
    pub(crate) fn timeout(&self) -> Duration {
        self.0.timeout
    }
    pub(crate) fn received(&self, image: Vec<u8>) {
        *self.0.image.lock().unwrap() = Some(image);
        self.0.ready.store(true, Ordering::Release);
    }
    /// All receiver clones must have been dropped after joined retirement.
    /// Keeping a live TLS owner makes this fail before any flash operation.
    pub fn stage(
        self,
        boot: &mut impl boot::Control,
        quiet: &mut impl wifi::QuiescenceControl,
    ) -> Result<(), crate::startup::Error> {
        use crate::startup::Error;
        let shared = Arc::try_unwrap(self.0).map_err(|_| Error::Boot(boot::DeviceError::Busy))?;
        let image = shared
            .image
            .into_inner()
            .map_err(|_| Error::Boot(boot::DeviceError::Io))?
            .ok_or(Error::Boot(boot::DeviceError::InvalidInput))?;
        if boot.state().map_err(Error::Boot)? != boot::State::Stable {
            return Err(Error::Boot(boot::DeviceError::Busy));
        }
        quiet.quiesce().map_err(Error::Wifi)?;
        let result = boot
            .stage_update(&mut image.as_slice(), image.len() as u32)
            .and_then(|()| boot.request_trial())
            .map_err(Error::Boot);
        let resumed = quiet.resume().map_err(Error::Wifi);
        result?;
        resumed?;
        if boot.state().map_err(Error::Boot)? != boot::State::Pending {
            return Err(Error::Boot(boot::DeviceError::InvalidState));
        }
        Ok(())
    }
}
pub(crate) struct Receiving<'a>(&'a Update);
impl Drop for Receiving<'_> {
    fn drop(&mut self) {
        self.0 .0.admission.store(0, Ordering::Release);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use boot::{Control, DeviceError, State};
    use std::{cell::RefCell, rc::Rc};
    struct Boot(State, Rc<RefCell<Vec<&'static str>>>, &'static str);
    impl Control for Boot {
        fn state(&mut self) -> Result<State, DeviceError> {
            Ok(self.0)
        }
        fn stage_update(
            &mut self,
            source: &mut dyn std::io::Read,
            length: u32,
        ) -> Result<(), DeviceError> {
            self.1.borrow_mut().push("stage");
            let mut image = Vec::new();
            source.read_to_end(&mut image).unwrap();
            assert_eq!(image, [42; 32]);
            assert_eq!(length, 32);
            if self.2 == "stage" {
                Err(DeviceError::Io)
            } else {
                Ok(())
            }
        }
        fn request_trial(&mut self) -> Result<(), DeviceError> {
            self.1.borrow_mut().push("trial");
            if self.2 == "trial" {
                return Err(DeviceError::Io);
            }
            self.0 = State::Pending;
            Ok(())
        }
        fn confirm(&mut self) -> Result<(), DeviceError> {
            panic!("no confirmation during staging")
        }
    }
    struct Quiet(Rc<RefCell<Vec<&'static str>>>, &'static str);
    impl wifi::QuiescenceControl for Quiet {
        fn quiesce(&mut self) -> Result<(), wifi::Error> {
            self.0.borrow_mut().push("quiet");
            if self.1 == "quiet" {
                Err(wifi::Error::Io)
            } else {
                Ok(())
            }
        }
        fn resume(&mut self) -> Result<(), wifi::Error> {
            self.0.borrow_mut().push("resume");
            if self.1 == "resume" {
                Err(wifi::Error::Io)
            } else {
                Ok(())
            }
        }
    }
    #[test]
    fn joined_receiver_quiescence_and_failure_ordering() {
        for (fault, expected) in [
            ("", vec!["quiet", "stage", "trial", "resume"]),
            ("quiet", vec!["quiet"]),
            ("stage", vec!["quiet", "stage", "resume"]),
            ("trial", vec!["quiet", "stage", "trial", "resume"]),
            ("resume", vec!["quiet", "stage", "trial", "resume"]),
        ] {
            let trace = Rc::new(RefCell::new(Vec::new()));
            let mut boot = Boot(State::Stable, trace.clone(), fault);
            let mut quiet = Quiet(trace.clone(), fault);
            let update = Update::new(32, Duration::from_secs(1)).unwrap();
            {
                let _receiving = update.begin().unwrap();
                assert!(!update.retire_if_idle());
                assert!(update.begin().is_none());
                assert!(!update.ready());
            }
            assert!(update.retire_if_idle());
            assert!(update.begin().is_none());
            let receiver = update.clone();
            assert!(update.renew().is_err());
            let update = receiver.renew().unwrap();
            assert!(update.begin().is_some());
            update.received(vec![42; 32]);
            assert!(update.ready());
            assert!(update.clone().stage(&mut boot, &mut quiet).is_err());
            assert!(trace.borrow().is_empty()); // A retained receiver prevents all flash operations.
            assert_eq!(
                update.stage(&mut boot, &mut quiet).is_ok(),
                fault.is_empty()
            );
            assert_eq!(*trace.borrow(), expected);
        }
    }
}

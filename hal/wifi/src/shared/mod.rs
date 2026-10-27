//! Shared Rust station ownership; SDK acquisition stays on the blocking worker.
#![deny(unsafe_code)]
use crate::worker::Driver;
use crate::{AccessPoint, Capabilities, CredentialStorage, Credentials, Error, LinkState, Ssid};
mod station;
pub(crate) use station::Station;
struct Native<B: Station> {
    state: Option<B>,
    storage: CredentialStorage,
}
impl<B: Station> Native<B> {
    fn state(&mut self) -> Result<&mut B, Error> {
        self.state.as_mut().ok_or(Error::Unavailable)
    }
}
impl<B: Station> Driver for Native<B> {
    fn check(&mut self) -> Result<(), Error> {
        if self.state.is_none() {
            self.state = Some(B::open(self.storage)?);
        }
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        B::capabilities(self.storage)
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        self.state()?.scan(emit)
    }
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        self.state()?.connect(ssid, credentials)
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        self.state()?.disconnect()
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        self.state()?.link()
    }
    fn close(&mut self) -> Result<(), Error> {
        if let Some(s) = &mut self.state {
            s.close()?;
        }
        self.state = None;
        Ok(())
    }
}
impl<B: Station> Drop for Native<B> {
    fn drop(&mut self) {
        if self.close().is_err() {
            // An uncertain association/reply retains its SDK resources and lease.
            if let Some(state) = self.state.take() {
                core::mem::forget(state);
            }
        }
    }
}
#[cfg_attr(test, allow(dead_code))]
pub(crate) fn open<B: Station, S>(
    sink: S,
    storage: CredentialStorage,
) -> Result<crate::worker::Worker, Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    crate::worker::Worker::start_with_stack(
        Native::<B> {
            state: None,
            storage,
        },
        sink,
        crate::worker::Lease::acquire()?,
        128 * 1024,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    static OPENS: AtomicUsize = AtomicUsize::new(0);
    static DROPS: AtomicUsize = AtomicUsize::new(0);
    static FAIL_CLOSE: AtomicBool = AtomicBool::new(false);
    struct Fake;
    impl Drop for Fake {
        fn drop(&mut self) {
            DROPS.fetch_add(1, Ordering::SeqCst);
        }
    }
    impl Station for Fake {
        fn open(_: CredentialStorage) -> Result<Self, Error> {
            OPENS.fetch_add(1, Ordering::SeqCst);
            Ok(Self)
        }
        fn capabilities(_: CredentialStorage) -> Capabilities {
            Capabilities {
                scan: true,
                open_network: true,
                wpa2_personal: true,
                credential_storage: CredentialStorage::Volatile,
            }
        }
        fn scan(&mut self, _: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
            Ok(true)
        }
        fn connect(&mut self, _: &Ssid, _: &Credentials) -> Result<(), Error> {
            Ok(())
        }
        fn disconnect(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn link(&mut self) -> Result<LinkState, Error> {
            Ok(LinkState::Addressed)
        }
        fn close(&mut self) -> Result<(), Error> {
            if FAIL_CLOSE.load(Ordering::SeqCst) {
                Err(Error::Io)
            } else {
                Ok(())
            }
        }
    }
    #[test]
    fn acquisition_is_lazy_and_failed_close_retains_owned_state() {
        OPENS.store(0, Ordering::SeqCst);
        DROPS.store(0, Ordering::SeqCst);
        let mut owner = Native::<Fake> {
            state: None,
            storage: CredentialStorage::Volatile,
        };
        assert_eq!(OPENS.load(Ordering::SeqCst), 0);
        assert_eq!(owner.link(), Err(Error::Unavailable));
        owner.check().unwrap();
        owner.check().unwrap();
        assert_eq!(OPENS.load(Ordering::SeqCst), 1);
        FAIL_CLOSE.store(true, Ordering::SeqCst);
        assert_eq!(owner.close(), Err(Error::Io));
        assert_eq!(DROPS.load(Ordering::SeqCst), 0);
        assert!(owner.state.is_some());
        FAIL_CLOSE.store(false, Ordering::SeqCst);
        owner.close().unwrap();
        assert_eq!(DROPS.load(Ordering::SeqCst), 1);
        drop(owner);
        assert_eq!(DROPS.load(Ordering::SeqCst), 1);
        let mut owner = Native::<Fake> {
            state: None,
            storage: CredentialStorage::Volatile,
        };
        owner.check().unwrap();
        FAIL_CLOSE.store(true, Ordering::SeqCst);
        drop(owner);
        assert_eq!(DROPS.load(Ordering::SeqCst), 1);
        FAIL_CLOSE.store(false, Ordering::SeqCst);
    }
}

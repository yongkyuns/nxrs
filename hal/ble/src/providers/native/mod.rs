//! Desktop scan normalization; lifecycle and consumer delivery use the common worker.
#![deny(unsafe_code)]

use crate::worker::{ScanReport, Scanner};
use crate::{Error, Uuid};

mod peers;

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
pub(crate) struct DesktopPeer;
#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
impl super::gatt::PeerResolver for DesktopPeer {
    fn resolve(peer: crate::gatt::Peer) -> Result<Option<[u8; 16]>, Error> {
        peers::resolve(peer).map(Some)
    }
}

mod scan;
#[cfg(all(nxrs_provider = "native", target_os = "linux"))]
use linux::State;
#[cfg(all(nxrs_provider = "native", target_os = "macos"))]
use macos::State;
use scan::{Report, ScanBackend};

struct Native<B> {
    bridge: Option<B>,
    filter: Option<Uuid>,
    session: Option<crate::SessionId>,
}

#[cfg(test)]
impl<B: ScanBackend> Native<B> {
    fn new(bridge: B) -> Self {
        Self {
            bridge: Some(bridge),
            filter: None,
            session: None,
        }
    }
}

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
pub(crate) fn open<S>(sink: S) -> Result<crate::worker::Worker, Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    crate::worker::Worker::start_with_stack(
        Native::<State> {
            bridge: None,
            filter: None,
            session: None,
        },
        sink,
        crate::worker::Lease::acquire()?,
        128 * 1024,
    )
}

impl<B: ScanBackend> Scanner for Native<B> {
    type Peer = [u8; 16];

    fn bind_session(&mut self, session: crate::SessionId) -> Result<(), Error> {
        peers::bind(session)?;
        self.session = Some(session);
        Ok(())
    }

    fn remember_peer(
        &mut self,
        session: crate::SessionId,
        id: crate::PeerId,
        peer: Self::Peer,
    ) -> Result<(), Error> {
        if self.session != Some(session) {
            return Err(Error::InvalidValue);
        }
        peers::remember(session, id, peer)
    }

    fn check(&mut self) -> Result<(), Error> {
        if self.bridge.is_none() {
            self.bridge = Some(B::open()?);
        }
        Ok(())
    }
    fn configure_filter(&mut self, service: Option<Uuid>) -> Result<(), Error> {
        self.filter = service;
        Ok(())
    }
    fn start_scan(&mut self) -> Result<(), Error> {
        self.bridge
            .as_mut()
            .ok_or(Error::Unavailable)?
            .start(self.filter)
    }
    fn get(
        &mut self,
        service: Option<Uuid>,
        output: &mut [ScanReport<Self::Peer>],
    ) -> Result<usize, Error> {
        if service != self.filter {
            return Err(Error::Io);
        }
        if output.is_empty() {
            return Ok(0);
        }
        self.bridge.as_mut().ok_or(Error::Unavailable)?.get(output)
    }

    fn stop_scan(&mut self) -> Result<(), Error> {
        self.bridge.as_mut().ok_or(Error::Unavailable)?.stop()
    }
    fn close(&mut self) {
        if let Some(mut bridge) = self.bridge.take() {
            bridge.close();
        }
        if let Some(session) = self.session.take() {
            peers::expire(session);
        }
    }
}

impl<B> Drop for Native<B> {
    fn drop(&mut self) {
        // Unexpected unwind cannot prove SDK quiescence. Explicit close takes it.
        if let Some(bridge) = self.bridge.take() {
            core::mem::forget(bridge);
        }
    }
}

#[cfg(all(nxrs_provider = "native", target_os = "macos"))]
#[allow(unsafe_code)]
pub(crate) mod macos;

#[cfg(all(nxrs_provider = "native", target_os = "linux"))]
#[allow(unsafe_code)]
pub(crate) mod linux;

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    struct Fake {
        filter: Option<Uuid>,
        fail_stop: bool,
        drops: Arc<AtomicUsize>,
    }
    impl Drop for Fake {
        fn drop(&mut self) {
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }
    impl ScanBackend for Fake {
        fn open() -> Result<Self, Error> {
            Err(Error::Unavailable)
        }
        fn start(&mut self, filter: Option<Uuid>) -> Result<(), Error> {
            self.filter = filter;
            Ok(())
        }
        fn get(&mut self, out: &mut [Report]) -> Result<usize, Error> {
            if let Some(row) = out.first_mut() {
                *row = Report {
                    peer: [7; 16],
                    rssi_dbm: None,
                    matches_filter: true,
                };
                Ok(1)
            } else {
                Ok(0)
            }
        }
        fn stop(&mut self) -> Result<(), Error> {
            if self.fail_stop {
                Err(Error::Io)
            } else {
                Ok(())
            }
        }
        fn close(&mut self) {}
    }
    #[test]
    fn failed_stop_keeps_state_and_filter_until_explicit_quiescent_close() {
        let drops = Arc::new(AtomicUsize::new(0));
        let mut driver = Native::new(Fake {
            filter: None,
            fail_stop: true,
            drops: drops.clone(),
        });
        let filter = Some(Uuid([5; 16]));
        driver.configure_filter(filter).unwrap();
        driver.start_scan().unwrap();
        assert_eq!(driver.bridge.as_ref().unwrap().filter, filter);
        let mut output = [Report::default(); 1];
        assert_eq!(driver.get(filter, &mut output), Ok(1));
        assert_eq!(output[0].peer, [7; 16]);
        assert_eq!(output[0].rssi_dbm, None);
        assert_eq!(driver.get(None, &mut output), Err(Error::Io));
        assert_eq!(driver.get(filter, &mut []), Ok(0));
        assert_eq!(driver.stop_scan(), Err(Error::Io));
        assert_eq!(drops.load(Ordering::SeqCst), 0);
        driver.bridge.as_mut().unwrap().fail_stop = false;
        driver.stop_scan().unwrap();
        driver.close();
        assert_eq!(drops.load(Ordering::SeqCst), 1);
        drop(driver);
        assert_eq!(drops.load(Ordering::SeqCst), 1);
    }
    #[test]
    fn unexpected_drop_does_not_free_callback_owned_state() {
        let drops = Arc::new(AtomicUsize::new(0));
        let driver = Native::new(Fake {
            filter: None,
            fail_stop: true,
            drops: drops.clone(),
        });
        drop(driver);
        assert_eq!(drops.load(Ordering::SeqCst), 0);
    }
}

//! Desktop scan normalization; lifecycle and consumer delivery use the common worker.
#![deny(unsafe_code)]

use crate::worker::{ScanReport, Scanner};
use crate::{Error, Uuid};

mod peers;

#[cfg(all(nxrs_provider = "native", target_os = "macos"))]
#[allow(unsafe_code)]
mod macos_scan;

#[cfg(all(not(test), nxrs_provider = "native", target_os = "macos"))]
#[allow(unsafe_code)]
mod macos_gatt;

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

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
mod ffi;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Row {
    peer: [u8; 16],
    rssi_dbm: i32,
    matches_filter: u32,
}

trait Bridge: Send + 'static {
    fn check(&mut self) -> Result<(), Error>;
    fn start(&mut self, service: Option<Uuid>) -> Result<(), Error>;
    fn get(&mut self, rows: &mut [Row]) -> Result<usize, Error>;
    fn stop(&mut self) -> Result<(), Error>;
    fn close(&mut self);
}

struct Native<B> {
    bridge: B,
    filter: Option<Uuid>,
    session: Option<crate::SessionId>,
}

impl<B: Bridge> Native<B> {
    fn new(bridge: B) -> Self {
        Self {
            bridge,
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
        Native::new(ffi::Bridge::new()),
        sink,
        crate::worker::Lease::acquire()?,
        128 * 1024,
    )
}

impl<B: Bridge> Scanner for Native<B> {
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
        self.bridge.check()
    }
    fn configure_filter(&mut self, service: Option<Uuid>) -> Result<(), Error> {
        self.filter = service;
        Ok(())
    }
    fn start_scan(&mut self) -> Result<(), Error> {
        self.bridge.start(self.filter)
    }
    fn get(
        &mut self,
        service: Option<Uuid>,
        output: &mut [ScanReport<Self::Peer>],
    ) -> Result<usize, Error> {
        if service != self.filter {
            return Err(Error::Io);
        }
        let mut rows = [Row::default(); 8];
        let capacity = output.len().min(rows.len());
        if capacity == 0 {
            return Ok(0);
        }
        let count = self.bridge.get(&mut rows[..capacity])?;
        if count > capacity {
            return Err(Error::Io);
        }
        for (source, destination) in rows.iter().zip(output.iter_mut()).take(count) {
            let rssi_dbm = match source.rssi_dbm {
                i32::MIN => None,
                value => Some(i16::try_from(value).map_err(|_| Error::Io)?),
            };
            let matches_filter = match source.matches_filter {
                0 => false,
                1 => true,
                _ => return Err(Error::Io),
            };
            *destination = ScanReport {
                peer: source.peer,
                rssi_dbm,
                matches_filter,
            };
        }
        Ok(count)
    }
    fn stop_scan(&mut self) -> Result<(), Error> {
        self.bridge.stop()
    }
    fn close(&mut self) {
        self.bridge.close();
        if let Some(session) = self.session.take() {
            peers::expire(session);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fake {
        row: Row,
        count: usize,
        filter: Option<Uuid>,
        stop: Result<(), Error>,
        closed: bool,
    }
    impl Bridge for Fake {
        fn check(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn start(&mut self, service: Option<Uuid>) -> Result<(), Error> {
            self.filter = service;
            Ok(())
        }
        fn get(&mut self, rows: &mut [Row]) -> Result<usize, Error> {
            rows[0] = self.row;
            Ok(self.count)
        }
        fn stop(&mut self) -> Result<(), Error> {
            self.stop
        }
        fn close(&mut self) {
            self.closed = true;
        }
    }
    fn native(row: Row) -> Native<Fake> {
        Native::new(Fake {
            row,
            count: 1,
            filter: None,
            stop: Ok(()),
            closed: false,
        })
    }

    #[test]
    fn opaque_identity_layout_and_unknown_rssi_are_preserved() {
        assert_eq!(core::mem::size_of::<Row>(), 24);
        assert_eq!(core::mem::align_of::<Row>(), 4);
        assert_eq!(core::mem::offset_of!(Row, rssi_dbm), 16);
        assert_eq!(core::mem::offset_of!(Row, matches_filter), 20);
        let mut driver = native(Row {
            peer: [7; 16],
            rssi_dbm: i32::MIN,
            matches_filter: 1,
        });
        let mut reports = [ScanReport::default(); 8];
        assert_eq!(driver.get(None, &mut reports), Ok(1));
        assert_eq!(reports[0].peer, [7; 16]);
        assert_eq!(reports[0].rssi_dbm, None);
        assert!(reports[0].matches_filter);
    }

    #[test]
    fn malformed_native_rows_and_counts_are_rejected() {
        let mut reports = [ScanReport::default(); 8];
        for row in [
            Row {
                rssi_dbm: i32::MAX,
                ..Row::default()
            },
            Row {
                matches_filter: 2,
                ..Row::default()
            },
        ] {
            assert_eq!(native(row).get(None, &mut reports), Err(Error::Io));
        }
        let mut driver = native(Row::default());
        driver.bridge.count = 9;
        assert_eq!(driver.get(None, &mut reports), Err(Error::Io));
        assert_eq!(driver.get(None, &mut []), Ok(0));
    }

    #[test]
    fn filter_is_applied_at_scan_start_and_stop_errors_remain_visible() {
        let mut driver = native(Row::default());
        let filter = Some(Uuid([5; 16]));
        driver.configure_filter(filter).unwrap();
        driver.start_scan().unwrap();
        assert_eq!(driver.bridge.filter, filter);
        assert_eq!(
            driver.get(None, &mut [ScanReport::default(); 1]),
            Err(Error::Io)
        );
        driver.bridge.stop = Err(Error::Io);
        assert_eq!(driver.stop_scan(), Err(Error::Io));
        assert!(!driver.bridge.closed);
        driver.bridge.stop = Ok(());
        driver.stop_scan().unwrap();
        driver.close();
        assert!(driver.bridge.closed);
    }
}

#[cfg(all(nxrs_provider = "native", target_os = "linux"))]
#[allow(unsafe_code)]
mod gio;
#[cfg(all(nxrs_provider = "native", target_os = "linux"))]
#[allow(unsafe_code)]
mod linux_identity;
#[cfg(all(nxrs_provider = "native", target_os = "linux"))]
#[allow(unsafe_code)]
mod linux_scan;

#[cfg(all(not(test), nxrs_provider = "native", target_os = "linux"))]
#[allow(unsafe_code)]
mod linux_gatt;

#[cfg(all(nxrs_provider = "native", target_os = "macos"))]
mod macos_uuid;

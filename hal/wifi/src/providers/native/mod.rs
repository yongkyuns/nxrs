//! Desktop normalization; the controller and blocking worker are unchanged.
#![deny(unsafe_code)]

use crate::worker::Driver;
use crate::{AccessPoint, Capabilities, Credentials, Error, LinkState, Security, Ssid};
use std::boxed::Box;
use std::vec;

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
mod ffi;

const SCAN_CAPACITY: usize = 64;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Row {
    ssid_length: u32,
    security: u32,
    rssi_dbm: i32,
    ssid: [u8; 32],
}

/// Synchronous OS calls retain no borrowed buffers or callbacks.
trait Bridge: Send + 'static {
    fn check(&mut self) -> Result<(), Error>;
    fn capabilities(&self) -> Capabilities;
    fn scan(&mut self, rows: &mut [Row]) -> Result<(usize, bool), Error>;
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error>;
    fn disconnect(&mut self) -> Result<(), Error>;
    fn link(&mut self) -> Result<LinkState, Error>;
    fn close(&mut self) -> Result<(), Error>;
}

struct Native<B> {
    bridge: B,
    rows: Box<[Row]>,
}

impl<B: Bridge> Native<B> {
    fn new(bridge: B) -> Self {
        Self {
            bridge,
            rows: vec![Row::default(); SCAN_CAPACITY].into_boxed_slice(),
        }
    }
}

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
pub(crate) fn open<S>(
    sink: S,
    storage: crate::CredentialStorage,
) -> Result<crate::worker::Worker, Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    let lease = crate::worker::Lease::acquire()?;
    // Platform-local acquisition supplies policy; shared FFI never infers the OS.
    let station = cfg!(target_os = "linux") || storage == crate::CredentialStorage::SystemManaged;
    let capabilities = Capabilities {
        scan: true,
        open_network: station,
        wpa2_personal: station,
        credential_storage: if cfg!(target_os = "macos") {
            crate::CredentialStorage::SystemManaged
        } else {
            crate::CredentialStorage::Volatile
        },
    };
    crate::worker::Worker::start_with_stack(
        Native::new(ffi::Bridge::new(storage, capabilities)),
        sink,
        lease,
        128 * 1024,
    )
}

impl<B: Bridge> Driver for Native<B> {
    fn check(&mut self) -> Result<(), Error> {
        self.bridge.check()
    }
    fn capabilities(&self) -> Capabilities {
        self.bridge.capabilities()
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        let (count, _truncated) = self.bridge.scan(&mut self.rows)?;
        let rows = self.rows.get(..count).ok_or(Error::Protocol)?;
        for row in rows {
            let ssid = row
                .ssid
                .get(..row.ssid_length as usize)
                .ok_or(Error::Protocol)?;
            let access_point = AccessPoint {
                ssid: if ssid.is_empty() {
                    None
                } else {
                    Some(Ssid::new(ssid).map_err(|_| Error::Protocol)?)
                },
                security: match row.security {
                    0 => Security::Open,
                    1 => Security::Wpa2Personal,
                    2 => Security::Other,
                    _ => return Err(Error::Protocol),
                },
                rssi_dbm: if row.rssi_dbm == i32::MIN {
                    None
                } else {
                    Some(i16::try_from(row.rssi_dbm).map_err(|_| Error::Protocol)?)
                },
            };
            if !emit(access_point) {
                return Ok(false);
            }
        }
        // The shared worker already labels scans best-effort/truncated. False
        // here means stopped delivery, not a completed bounded native scan.
        Ok(true)
    }
    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        self.bridge.connect(ssid, credentials)
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        self.bridge.disconnect()
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        self.bridge.link()
    }
    fn close(&mut self) -> Result<(), Error> {
        self.bridge.close()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::vec::Vec;

    struct Fake {
        row: Row,
        count: usize,
        truncated: bool,
        disconnects: usize,
    }
    impl Bridge for Fake {
        fn check(&mut self) -> Result<(), Error> {
            Ok(())
        }
        fn capabilities(&self) -> Capabilities {
            Capabilities {
                scan: true,
                open_network: true,
                wpa2_personal: true,
                credential_storage: crate::CredentialStorage::Volatile,
            }
        }
        fn scan(&mut self, rows: &mut [Row]) -> Result<(usize, bool), Error> {
            rows[0] = self.row;
            Ok((self.count, self.truncated))
        }
        fn connect(&mut self, _: &Ssid, _: &Credentials) -> Result<(), Error> {
            Err(Error::Busy)
        }
        fn disconnect(&mut self) -> Result<(), Error> {
            self.disconnects += 1;
            Ok(())
        }
        fn link(&mut self) -> Result<LinkState, Error> {
            Ok(LinkState::Addressed)
        }
        fn close(&mut self) -> Result<(), Error> {
            Ok(())
        }
    }
    fn driver(row: Row) -> Native<Fake> {
        Native::new(Fake {
            row,
            count: 1,
            truncated: false,
            disconnects: 0,
        })
    }

    #[test]
    fn bounded_rows_preserve_hidden_ssids_and_security_without_identifiers() {
        assert_eq!(core::mem::size_of::<Row>(), 44);
        assert_eq!(core::mem::align_of::<Row>(), 4);
        assert_eq!(core::mem::offset_of!(Row, security), 4);
        assert_eq!(core::mem::offset_of!(Row, rssi_dbm), 8);
        assert_eq!(core::mem::offset_of!(Row, ssid), 12);
        let mut native = driver(Row {
            security: 1,
            rssi_dbm: -40,
            ..Row::default()
        });
        let mut points = Vec::new();
        assert_eq!(
            native.scan(&mut |point| {
                points.push(point);
                true
            }),
            Ok(true)
        );
        assert_eq!(
            points,
            [AccessPoint {
                ssid: None,
                security: Security::Wpa2Personal,
                rssi_dbm: Some(-40)
            }]
        );
        native.bridge.truncated = true;
        assert_eq!(native.scan(&mut |_| true), Ok(true));
        native.bridge.count = SCAN_CAPACITY + 1;
        assert_eq!(native.scan(&mut |_| true), Err(Error::Protocol));
    }

    #[test]
    fn unavailable_signal_strength_is_not_invented_from_percent() {
        let mut native = driver(Row {
            rssi_dbm: i32::MIN,
            ..Row::default()
        });
        let mut points = Vec::new();
        assert_eq!(
            native.scan(&mut |point| {
                points.push(point);
                true
            }),
            Ok(true)
        );
        assert_eq!(points[0].rssi_dbm, None);
    }

    #[test]
    fn malformed_rows_and_stopped_delivery_are_not_complete_scans() {
        for row in [
            Row {
                ssid_length: 33,
                ..Row::default()
            },
            Row {
                security: 3,
                ..Row::default()
            },
            Row {
                rssi_dbm: i32::MAX,
                ..Row::default()
            },
        ] {
            assert_eq!(driver(row).scan(&mut |_| true), Err(Error::Protocol));
        }
        assert_eq!(driver(Row::default()).scan(&mut |_| false), Ok(false));
    }

    #[test]
    fn observation_and_close_do_not_request_disassociation() {
        let mut native = driver(Row::default());
        assert_eq!(native.link(), Ok(LinkState::Addressed));
        assert_eq!(
            native.connect(&Ssid::new(b"fixture").unwrap(), &Credentials::Open),
            Err(Error::Busy)
        );
        assert_eq!(native.close(), Ok(()));
        assert_eq!(native.bridge.disconnects, 0);
        assert_eq!(native.disconnect(), Ok(()));
        assert_eq!(native.bridge.disconnects, 1);
    }
}

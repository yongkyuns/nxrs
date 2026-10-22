//! CoreWLAN session ownership, selection, rollback and deadlines in Rust.
use super::Station;
use crate::{
    AccessPoint, Capabilities, CredentialStorage, Credentials, Error, LinkState, Security, Ssid,
};
use core::ffi::c_void;
use std::prelude::v1::*;
use std::{
    net::{Ipv4Addr, Ipv6Addr},
    ptr::NonNull,
    thread,
    time::{Duration, Instant},
};
const UNSUPPORTED: Error = Error::Unsupported;
const BUSY: Error = Error::Busy;
const UNAVAILABLE: Error = Error::Unavailable;
const DENIED: Error = Error::PermissionDenied;
const TIMEOUT: Error = Error::Timeout;
const PROTOCOL: Error = Error::Protocol;
const IO: Error = Error::Io;
#[repr(C)]
struct Candidate {
    length: u64,
    rssi: i64,
    security: u32,
    bssid_length: u32,
    ssid: [u8; 32],
    bssid: [u8; 64],
}
unsafe extern "C" {
    fn macos_wifi_permission() -> i32;
    fn macos_wifi_open(context: *mut *mut c_void) -> i32;
    fn macos_wifi_release(object: *mut c_void);
    fn macos_wifi_state(
        context: *mut c_void,
        powered: *mut u32,
        mode: *mut u32,
        bssid: *mut u8,
        length: *mut u32,
    ) -> i32;
    fn macos_wifi_addresses(
        context: *mut c_void,
        arg: *mut c_void,
        emit: unsafe extern "C" fn(*mut c_void, u32, u32, *const u8),
    ) -> i32;
    fn macos_wifi_scan(
        context: *mut c_void,
        ssid: *const u8,
        length: usize,
        snapshot: *mut *mut c_void,
    ) -> i32;
    fn macos_wifi_count(snapshot: *mut c_void) -> usize;
    fn macos_wifi_row(snapshot: *mut c_void, index: usize, row: *mut Candidate) -> i32;
    fn macos_wifi_associate(
        context: *mut c_void,
        snapshot: *mut c_void,
        index: usize,
        password: *const u8,
        length: usize,
    ) -> i32;
    fn macos_wifi_disassociate(context: *mut c_void);
}
struct Sdk(NonNull<c_void>);
impl Drop for Sdk {
    fn drop(&mut self) {
        unsafe { macos_wifi_release(self.0.as_ptr()) };
    }
}
pub(crate) struct State {
    sdk: Sdk,
    storage: CredentialStorage,
    owned: Option<std::vec::Vec<u8>>,
    connect_timeout: Duration,
}
fn check(code: i32) -> Result<(), Error> {
    if code == 0 {
        Ok(())
    } else {
        Err(match code {
            1 => Error::Unsupported,
            2 => Error::Busy,
            3 => Error::Unavailable,
            4 => Error::PermissionDenied,
            5 => Error::AuthenticationFailed,
            6 => Error::InvalidConfig,
            7 => Error::Timeout,
            8 => Error::Protocol,
            9 => Error::Io,
            _ => Error::Protocol,
        })
    }
}
impl Sdk {
    fn snapshot(&self, filter: Option<&[u8]>) -> Result<Sdk, Error> {
        let mut raw = core::ptr::null_mut();
        let (data, len) = filter.map_or((core::ptr::null(), 0), |f| (f.as_ptr(), f.len()));
        check(unsafe { macos_wifi_scan(self.0.as_ptr(), data, len, &mut raw) })?;
        Ok(Sdk(NonNull::new(raw).ok_or(PROTOCOL)?))
    }
    fn row(&self, index: usize) -> Result<Candidate, Error> {
        let mut row = Candidate {
            length: 0,
            rssi: 0,
            security: 0,
            bssid_length: 0,
            ssid: [0; 32],
            bssid: [0; 64],
        };
        check(unsafe { macos_wifi_row(self.0.as_ptr(), index, &mut row) })?;
        if row.length > 32 || row.bssid_length > 64 || i16::try_from(row.rssi).is_err() {
            return Err(PROTOCOL);
        }
        Ok(row)
    }
}
impl State {
    fn interface(&self) -> Result<(bool, u32, std::vec::Vec<u8>), Error> {
        let (mut powered, mut mode, mut length) = (0, 0, 0);
        let mut bssid = [0; 64];
        check(unsafe {
            macos_wifi_state(
                self.sdk.0.as_ptr(),
                &mut powered,
                &mut mode,
                bssid.as_mut_ptr(),
                &mut length,
            )
        })?;
        if length > 64 || powered > 1 || mode > 2 {
            return Err(PROTOCOL);
        }
        Ok((powered != 0, mode, bssid[..length as usize].to_vec()))
    }
    fn addressed(&self) -> Result<bool, Error> {
        unsafe extern "C" fn visit(arg: *mut c_void, family: u32, up: u32, data: *const u8) {
            if up == 0 || data.is_null() {
                return;
            }
            let valid = match family {
                4 => {
                    let bytes = unsafe { core::slice::from_raw_parts(data, 4) };
                    let ip = Ipv4Addr::new(bytes[0], bytes[1], bytes[2], bytes[3]);
                    !ip.is_unspecified()
                        && !ip.is_loopback()
                        && !ip.is_link_local()
                        && bytes[0] < 224
                }
                6 => {
                    let mut bytes = [0; 16];
                    unsafe { core::ptr::copy_nonoverlapping(data, bytes.as_mut_ptr(), 16) };
                    let ip = Ipv6Addr::from(bytes);
                    !ip.is_unspecified()
                        && !ip.is_loopback()
                        && !ip.is_unicast_link_local()
                        && !ip.is_multicast()
                }
                _ => false,
            };
            if valid {
                unsafe { *arg.cast::<bool>() = true };
            }
        }
        let mut addressed = false;
        check(unsafe {
            macos_wifi_addresses(
                self.sdk.0.as_ptr(),
                (&mut addressed as *mut bool).cast(),
                visit,
            )
        })?;
        Ok(addressed)
    }
    fn link_state(&self) -> Result<LinkState, Error> {
        let (powered, mode, _) = self.interface()?;
        if !powered || mode == 0 {
            return Ok(LinkState::Disconnected);
        }
        if mode != 1 {
            return Err(BUSY);
        }
        Ok(if self.addressed()? {
            LinkState::Addressed
        } else {
            LinkState::Associated
        })
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        let (_, mode, bssid) = self.interface()?;
        if mode == 0 {
            self.owned = None;
            return Ok(());
        }
        let owned = self.owned.as_ref().ok_or(BUSY)?;
        check(unsafe { macos_wifi_permission() })?;
        if bssid != *owned {
            return Err(BUSY);
        }
        unsafe { macos_wifi_disassociate(self.sdk.0.as_ptr()) };
        let deadline = Instant::now() + Duration::from_secs(2);
        while self.interface()?.1 != 0 {
            if Instant::now() >= deadline {
                return Err(IO);
            }
            thread::sleep(Duration::from_millis(20));
        }
        self.owned = None;
        Ok(())
    }
    fn associate(&mut self, ssid: &[u8], password: &[u8]) -> Result<(), Error> {
        if self.storage != CredentialStorage::SystemManaged {
            return Err(UNSUPPORTED);
        }
        if !self.interface()?.0 {
            return Err(UNAVAILABLE);
        }
        check(unsafe { macos_wifi_permission() })?;
        if self.addressed()? || self.interface()?.1 != 0 {
            return Err(BUSY);
        }
        let snapshot = self.sdk.snapshot(Some(ssid))?;
        let mut best: Option<(usize, Candidate)> = None;
        for i in 0..unsafe { macos_wifi_count(snapshot.0.as_ptr()) } {
            let row = snapshot.row(i)?;
            if &row.ssid[..row.length as usize] != ssid
                || row.security & (if password.is_empty() { 1 } else { 2 }) == 0
            {
                continue;
            }
            if best.as_ref().is_none_or(|(_, old)| row.rssi > old.rssi) {
                best = Some((i, row));
            }
        }
        let (index, selected) = best.ok_or(UNAVAILABLE)?;
        if selected.bssid_length == 0 {
            return Err(DENIED);
        }
        if self.addressed()? || self.interface()?.1 != 0 {
            return Err(BUSY);
        }
        self.owned = Some(selected.bssid[..selected.bssid_length as usize].to_vec());
        let mut result = check(unsafe {
            macos_wifi_associate(
                self.sdk.0.as_ptr(),
                snapshot.0.as_ptr(),
                index,
                password.as_ptr(),
                password.len(),
            )
        });
        if result.is_ok() {
            let deadline = Instant::now() + self.connect_timeout;
            result = loop {
                match self.link_state() {
                    Err(e) => break Err(e),
                    Ok(link) => {
                        match self.interface() {
                            Err(e) => break Err(e),
                            Ok((_, _, bssid)) => {
                                if bssid != *self.owned.as_ref().unwrap() {
                                    break Err(UNAVAILABLE);
                                }
                            }
                        }
                        if link == LinkState::Addressed {
                            return Ok(());
                        }
                    }
                }
                if Instant::now() >= deadline {
                    break Err(TIMEOUT);
                }
                thread::sleep(Duration::from_millis(20));
            };
        }
        match self.disconnect() {
            Ok(()) => result,
            Err(_) => Err(IO),
        }
    }
}

// SAFETY: the private worker is the sole owner/caller. SDK state is acquired
// on that worker and never transferred after initialization.
unsafe impl Send for State {}

impl Station for State {
    fn open(storage: CredentialStorage) -> Result<Self, Error> {
        let mut raw = core::ptr::null_mut();
        check(unsafe { macos_wifi_open(&mut raw) })?;
        let state = Self {
            sdk: Sdk(NonNull::new(raw).ok_or(Error::Protocol)?),
            storage,
            owned: None,
            connect_timeout: if cfg!(all(test, native_witness)) {
                Duration::from_millis(50)
            } else {
                Duration::from_secs(20)
            },
        };
        if !state.interface()?.0 {
            return Err(Error::Unavailable);
        }
        Ok(state)
    }
    fn capabilities(storage: CredentialStorage) -> Capabilities {
        let station = storage == CredentialStorage::SystemManaged;
        Capabilities {
            scan: true,
            open_network: station,
            wpa2_personal: station,
            credential_storage: CredentialStorage::SystemManaged,
        }
    }
    fn scan(&mut self, emit: &mut dyn FnMut(AccessPoint) -> bool) -> Result<bool, Error> {
        check(unsafe { macos_wifi_permission() })?;
        let snapshot = self.sdk.snapshot(None)?;
        let count = unsafe { macos_wifi_count(snapshot.0.as_ptr()) };
        for i in 0..count.min(64) {
            let row = snapshot.row(i)?;
            let point = AccessPoint {
                ssid: if row.length == 0 {
                    None
                } else {
                    Some(Ssid::new(&row.ssid[..row.length as usize])?)
                },
                security: if row.security & 1 != 0 {
                    Security::Open
                } else if row.security & 2 != 0 {
                    Security::Wpa2Personal
                } else {
                    Security::Other
                },
                rssi_dbm: Some(row.rssi as i16),
            };
            if !emit(point) {
                return Ok(false);
            }
        }
        Ok(count <= 64)
    }
    fn disconnect(&mut self) -> Result<(), Error> {
        State::disconnect(self)
    }
    fn link(&mut self) -> Result<LinkState, Error> {
        self.link_state()
    }
    fn close(&mut self) -> Result<(), Error> {
        if let Some(owned) = &self.owned {
            let bssid = self.interface()?.2;
            if !bssid.is_empty() && bssid != *owned {
                self.owned = None;
            } else {
                self.disconnect()?;
            }
        }
        Ok(())
    }

    fn connect(&mut self, ssid: &Ssid, credentials: &Credentials) -> Result<(), Error> {
        let password = match credentials {
            Credentials::Open => &[][..],
            Credentials::Wpa2Personal(p) => p.as_bytes(),
        };
        self.associate(ssid.as_bytes(), password)
    }
}

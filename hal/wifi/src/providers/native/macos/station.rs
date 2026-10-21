//! CoreWLAN session ownership, selection, rollback and deadlines in Rust.
use super::Row;
use core::ffi::c_void;
use std::prelude::v1::*;
use std::{
    net::{Ipv4Addr, Ipv6Addr},
    ptr::NonNull,
    thread,
    time::{Duration, Instant},
};
const OK: i32 = 0;
const UNSUPPORTED: i32 = 1;
const BUSY: i32 = 2;
const UNAVAILABLE: i32 = 3;
const DENIED: i32 = 4;
const INVALID: i32 = 6;
const TIMEOUT: i32 = 7;
const PROTOCOL: i32 = 8;
const IO: i32 = 9;
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
    fn nxrs_macos_wifi_permission() -> i32;
    fn nxrs_macos_wifi_open(context: *mut *mut c_void) -> i32;
    fn nxrs_macos_wifi_release(object: *mut c_void);
    fn nxrs_macos_wifi_state(
        context: *mut c_void,
        powered: *mut u32,
        mode: *mut u32,
        bssid: *mut u8,
        length: *mut u32,
    ) -> i32;
    fn nxrs_macos_wifi_addresses(
        context: *mut c_void,
        arg: *mut c_void,
        emit: unsafe extern "C" fn(*mut c_void, u32, u32, *const u8),
    ) -> i32;
    fn nxrs_macos_wifi_scan(
        context: *mut c_void,
        ssid: *const u8,
        length: usize,
        snapshot: *mut *mut c_void,
    ) -> i32;
    fn nxrs_macos_wifi_count(snapshot: *mut c_void) -> usize;
    fn nxrs_macos_wifi_row(snapshot: *mut c_void, index: usize, row: *mut Candidate) -> i32;
    fn nxrs_macos_wifi_associate(
        context: *mut c_void,
        snapshot: *mut c_void,
        index: usize,
        password: *const u8,
        length: usize,
    ) -> i32;
    fn nxrs_macos_wifi_disassociate(context: *mut c_void);
}
struct Sdk(NonNull<c_void>);
impl Drop for Sdk {
    fn drop(&mut self) {
        unsafe { nxrs_macos_wifi_release(self.0.as_ptr()) };
    }
}
struct State {
    sdk: Sdk,
    storage: u32,
    owned: Option<std::vec::Vec<u8>>,
    connect_timeout: Duration,
}
fn check(code: i32) -> Result<(), i32> {
    if code == 0 {
        Ok(())
    } else {
        Err(code)
    }
}
impl Sdk {
    fn snapshot(&self, filter: Option<&[u8]>) -> Result<Sdk, i32> {
        let mut raw = core::ptr::null_mut();
        let (data, len) = filter.map_or((core::ptr::null(), 0), |f| (f.as_ptr(), f.len()));
        check(unsafe { nxrs_macos_wifi_scan(self.0.as_ptr(), data, len, &mut raw) })?;
        Ok(Sdk(NonNull::new(raw).ok_or(PROTOCOL)?))
    }
    fn row(&self, index: usize) -> Result<Candidate, i32> {
        let mut row = Candidate {
            length: 0,
            rssi: 0,
            security: 0,
            bssid_length: 0,
            ssid: [0; 32],
            bssid: [0; 64],
        };
        check(unsafe { nxrs_macos_wifi_row(self.0.as_ptr(), index, &mut row) })?;
        if row.length > 32 || row.bssid_length > 64 || i16::try_from(row.rssi).is_err() {
            return Err(PROTOCOL);
        }
        Ok(row)
    }
}
impl State {
    fn interface(&self) -> Result<(bool, u32, std::vec::Vec<u8>), i32> {
        let (mut powered, mut mode, mut length) = (0, 0, 0);
        let mut bssid = [0; 64];
        check(unsafe {
            nxrs_macos_wifi_state(
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
    fn addressed(&self) -> Result<bool, i32> {
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
            nxrs_macos_wifi_addresses(
                self.sdk.0.as_ptr(),
                (&mut addressed as *mut bool).cast(),
                visit,
            )
        })?;
        Ok(addressed)
    }
    fn link(&self) -> Result<u32, i32> {
        let (powered, mode, _) = self.interface()?;
        if !powered || mode == 0 {
            return Ok(1);
        }
        if mode != 1 {
            return Err(BUSY);
        }
        Ok(if self.addressed()? { 3 } else { 2 })
    }
    fn disconnect(&mut self) -> Result<(), i32> {
        let (_, mode, bssid) = self.interface()?;
        if mode == 0 {
            self.owned = None;
            return Ok(());
        }
        let owned = self.owned.as_ref().ok_or(BUSY)?;
        check(unsafe { nxrs_macos_wifi_permission() })?;
        if bssid != *owned {
            return Err(BUSY);
        }
        unsafe { nxrs_macos_wifi_disassociate(self.sdk.0.as_ptr()) };
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
    fn connect(&mut self, ssid: &[u8], password: &[u8]) -> Result<(), i32> {
        if self.storage != 1 {
            return Err(UNSUPPORTED);
        }
        if !self.interface()?.0 {
            return Err(UNAVAILABLE);
        }
        check(unsafe { nxrs_macos_wifi_permission() })?;
        if self.addressed()? || self.interface()?.1 != 0 {
            return Err(BUSY);
        }
        let snapshot = self.sdk.snapshot(Some(ssid))?;
        let mut best: Option<(usize, Candidate)> = None;
        for i in 0..unsafe { nxrs_macos_wifi_count(snapshot.0.as_ptr()) } {
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
            nxrs_macos_wifi_associate(
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
                match self.link() {
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
                        if link == 3 {
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
// Private compatibility symbols consumed by the selected Rust adapter. The
// state itself is Rust-owned; native glue never retains these Box pointers.
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_open(output: *mut *mut c_void, storage: u32) -> i32 {
    unsafe {
        open_timed(
            output,
            storage,
            if cfg!(test) {
                Duration::from_millis(50)
            } else {
                Duration::from_secs(20)
            },
        )
    }
}
pub(super) unsafe fn open_timed(output: *mut *mut c_void, storage: u32, timeout: Duration) -> i32 {
    if output.is_null() {
        return INVALID;
    }
    unsafe { *output = core::ptr::null_mut() };
    if storage > 1 {
        return INVALID;
    }
    let mut raw = core::ptr::null_mut();
    let code = unsafe { nxrs_macos_wifi_open(&mut raw) };
    if code != 0 {
        return code;
    }
    let Some(raw) = NonNull::new(raw) else {
        return PROTOCOL;
    };
    let state = State {
        sdk: Sdk(raw),
        storage,
        owned: None,
        connect_timeout: timeout,
    };
    match state.interface() {
        Ok((true, _, _)) => {}
        Ok(_) => return UNAVAILABLE,
        Err(e) => return e,
    }
    unsafe { *output = Box::into_raw(Box::new(state)).cast() };
    OK
}
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_scan(
    context: *mut c_void,
    rows: *mut Row,
    capacity: u32,
    count: *mut u32,
    truncated: *mut u32,
) -> i32 {
    if context.is_null()
        || rows.is_null()
        || capacity == 0
        || count.is_null()
        || truncated.is_null()
    {
        return INVALID;
    }
    unsafe {
        *count = 0;
        *truncated = 0
    };
    let state = unsafe { &*context.cast::<State>() };
    let permission = unsafe { nxrs_macos_wifi_permission() };
    if permission != 0 {
        return permission;
    }
    let snapshot = match state.sdk.snapshot(None) {
        Ok(s) => s,
        Err(e) => return e,
    };
    let total = unsafe { nxrs_macos_wifi_count(snapshot.0.as_ptr()) };
    for i in 0..total.min(capacity as usize) {
        let candidate = match snapshot.row(i) {
            Ok(row) => row,
            Err(e) => return e,
        };
        let row = Row {
            ssid_length: candidate.length as u32,
            security: if candidate.security & 1 != 0 {
                0
            } else if candidate.security & 2 != 0 {
                1
            } else {
                2
            },
            rssi_dbm: candidate.rssi as i32,
            ssid: candidate.ssid,
        };
        unsafe {
            rows.add(i).write(row);
            *count += 1
        };
    }
    unsafe { *truncated = u32::from(total > capacity as usize) };
    OK
}
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_link(context: *mut c_void, link: *mut u32) -> i32 {
    if context.is_null() || link.is_null() {
        return INVALID;
    }
    match unsafe { &*context.cast::<State>() }.link() {
        Ok(value) => {
            unsafe { *link = value };
            OK
        }
        Err(e) => e,
    }
}
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_disconnect(context: *mut c_void) -> i32 {
    if context.is_null() {
        return INVALID;
    }
    match unsafe { &mut *context.cast::<State>() }.disconnect() {
        Ok(()) => OK,
        Err(e) => e,
    }
}
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_connect(
    context: *mut c_void,
    ssid: *const u8,
    len: usize,
    password: *const u8,
    password_len: usize,
) -> i32 {
    if context.is_null()
        || ssid.is_null()
        || len == 0
        || len > 32
        || password_len > 63
        || (password_len != 0 && (password.is_null() || password_len < 8))
    {
        return INVALID;
    }
    let password = if password_len == 0 {
        &[][..]
    } else {
        unsafe { core::slice::from_raw_parts(password, password_len) }
    };
    let result = unsafe { &mut *context.cast::<State>() }
        .connect(unsafe { core::slice::from_raw_parts(ssid, len) }, password);
    match result {
        Ok(()) => OK,
        Err(e) => e,
    }
}
#[no_mangle]
pub(super) unsafe extern "C" fn nxrs_host_wifi_release(context: *mut c_void) -> i32 {
    if context.is_null() {
        return OK;
    }
    let state = unsafe { &mut *context.cast::<State>() };
    if let Some(owned) = &state.owned {
        let bssid = match state.interface() {
            Ok((_, _, b)) => b,
            Err(e) => return e,
        };
        if !bssid.is_empty() && bssid != *owned {
            state.owned = None;
        } else if let Err(e) = state.disconnect() {
            return e;
        }
    }
    unsafe { drop(Box::from_raw(context.cast::<State>())) };
    OK
}

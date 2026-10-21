//! CoreBluetooth scan policy. SDK callbacks borrow Reports until the serial detach barrier.
use super::{macos_uuid::canonical, Row};
use std::prelude::v1::*;
use std::{
    collections::VecDeque,
    ffi::c_void,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
unsafe extern "C" {
    fn nxrs_macos_scan_authorization() -> i32;
    fn nxrs_macos_scan_context(arg: *mut c_void) -> *mut c_void;
    fn nxrs_macos_scan_manager(context: *mut c_void) -> *mut c_void;
    fn nxrs_macos_scan_status(context: *mut c_void) -> i32;
    fn nxrs_macos_scan_start(context: *mut c_void) -> bool;
    fn nxrs_macos_scan_stop(context: *mut c_void) -> bool;
    fn nxrs_macos_scan_detach(context: *mut c_void);
    fn nxrs_macos_scan_release(context: *mut c_void);
}
struct Reports {
    manager: usize,
    scanning: bool,
    filter: Option<[u8; 16]>,
    pending: Option<Row>,
    matched: bool,
    rows: VecDeque<Row>,
}
struct State {
    sdk: *mut c_void,
    reports: Arc<Mutex<Reports>>,
    manager: bool,
}
impl State {
    fn ready(&mut self) -> i32 {
        let permission = unsafe { nxrs_macos_scan_authorization() };
        if permission != 0 {
            return permission;
        }
        if !self.manager {
            let manager = unsafe { nxrs_macos_scan_manager(self.sdk) };
            self.reports.lock().unwrap().manager = manager as usize;
            self.manager = true;
        }
        let start = Instant::now();
        loop {
            let status = unsafe { nxrs_macos_scan_status(self.sdk) };
            if status != 2 {
                return status;
            }
            if start.elapsed() >= Duration::from_millis(if cfg!(test) { 50 } else { 5000 }) {
                return 6;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn detach(&mut self) {
        unsafe { nxrs_macos_scan_detach(self.sdk) };
        self.manager = false;
        let mut r = self.reports.lock().unwrap();
        r.scanning = false;
        r.manager = 0;
        r.pending = None;
        r.rows.clear();
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_macos_scan_begin_rust(
    arg: *mut c_void,
    manager: *mut c_void,
    peer: *const u8,
    rssi: i64,
    valid: bool,
) {
    let mut r = unsafe { &*arg.cast::<Mutex<Reports>>() }.lock().unwrap();
    r.pending = None;
    r.matched = r.filter.is_none();
    if !r.scanning || r.manager != manager as usize {
        return;
    }
    let mut row = Row {
        peer: [0; 16],
        rssi_dbm: i32::MIN,
        matches_filter: 1,
    };
    unsafe { core::ptr::copy_nonoverlapping(peer, row.peer.as_mut_ptr(), 16) };
    if valid && rssi != 127 && i16::try_from(rssi).is_ok() {
        row.rssi_dbm = rssi as i32;
    }
    r.pending = Some(row);
}
#[no_mangle]
unsafe extern "C" fn nxrs_macos_scan_uuid_rust(arg: *mut c_void, data: *const u8, length: usize) {
    if length == 0 {
        return;
    }
    let mut r = unsafe { &*arg.cast::<Mutex<Reports>>() }.lock().unwrap();
    if r.filter.is_some()
        && canonical(unsafe { core::slice::from_raw_parts(data, length) }) == r.filter
    {
        r.matched = true;
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_macos_scan_end_rust(arg: *mut c_void) {
    let mut r = unsafe { &*arg.cast::<Mutex<Reports>>() }.lock().unwrap();
    if let Some(row) = r.pending.take() {
        if !r.matched {
            return;
        }
        if let Some(old) = r.rows.iter_mut().find(|old| old.peer == row.peer) {
            *old = row;
        } else if r.rows.len() < 32 {
            r.rows.push_back(row);
        }
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_open(out: *mut *mut c_void) -> i32 {
    if out.is_null() {
        return 5;
    }
    unsafe { *out = core::ptr::null_mut() };
    let permission = unsafe { nxrs_macos_scan_authorization() };
    if permission != 0 {
        return permission;
    }
    let reports = Arc::new(Mutex::new(Reports {
        manager: 0,
        scanning: false,
        filter: None,
        pending: None,
        matched: false,
        rows: VecDeque::with_capacity(32),
    }));
    let sdk = unsafe { nxrs_macos_scan_context(Arc::as_ptr(&reports).cast_mut().cast()) };
    if sdk.is_null() {
        return 7;
    }
    let mut state = Box::new(State {
        sdk,
        reports,
        manager: false,
    });
    let status = state.ready();
    if status != 0 {
        state.detach();
        unsafe { nxrs_macos_scan_release(sdk) };
        return status;
    }
    unsafe { *out = Box::into_raw(state).cast() };
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_start(context: *mut c_void, service: *const u8) -> i32 {
    if context.is_null() {
        return 5;
    }
    let state = unsafe { &mut *context.cast::<State>() };
    let status = state.ready();
    if status != 0 {
        return status;
    }
    {
        let mut r = state.reports.lock().unwrap();
        if r.scanning {
            return 2;
        }
        r.rows.clear();
        r.filter = if service.is_null() {
            None
        } else {
            let mut b = [0; 16];
            unsafe { core::ptr::copy_nonoverlapping(service, b.as_mut_ptr(), 16) };
            Some(b)
        };
        r.scanning = true;
    }
    if unsafe { nxrs_macos_scan_start(state.sdk) } {
        0
    } else {
        state.reports.lock().unwrap().scanning = false;
        7
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_get(
    context: *mut c_void,
    rows: *mut Row,
    capacity: u32,
    count: *mut u32,
) -> i32 {
    if context.is_null() || rows.is_null() || capacity == 0 || count.is_null() {
        return 5;
    }
    unsafe { *count = 0 };
    let state = unsafe { &mut *context.cast::<State>() };
    if !state.reports.lock().unwrap().scanning {
        return 3;
    }
    let status = unsafe { nxrs_macos_scan_status(state.sdk) };
    if status != 0 {
        return status;
    }
    let mut r = state.reports.lock().unwrap();
    let n = (capacity as usize).min(r.rows.len());
    for i in 0..n {
        unsafe { rows.add(i).write(r.rows.pop_front().unwrap()) };
    }
    unsafe { *count = n as u32 };
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_stop(context: *mut c_void) -> i32 {
    if context.is_null() {
        return 5;
    }
    let state = unsafe { &mut *context.cast::<State>() };
    if !unsafe { nxrs_macos_scan_stop(state.sdk) } {
        return 7;
    }
    state.detach();
    0
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_release(context: *mut c_void) {
    if context.is_null() {
        return;
    }
    let state = unsafe { &mut *context.cast::<State>() };
    if !unsafe { nxrs_macos_scan_stop(state.sdk) } {
        return;
    }
    state.detach();
    unsafe {
        nxrs_macos_scan_release(state.sdk);
        drop(Box::from_raw(context.cast::<State>()));
    }
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_sdk(context: *mut c_void) -> *mut c_void {
    unsafe { &*context.cast::<State>() }.sdk
}
#[cfg(test)]
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_uuid(
    data: *const u8,
    len: usize,
    service: *const u8,
) -> bool {
    canonical(unsafe { core::slice::from_raw_parts(data, len) })
        .is_some_and(|uuid| uuid == unsafe { core::slice::from_raw_parts(service, 16) })
}

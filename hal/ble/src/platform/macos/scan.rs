//! CoreBluetooth scan policy. SDK callbacks borrow Reports until the serial detach barrier.
use super::ScanBackend;
use super::{uuid::canonical, Report};
use crate::{Error, Uuid};
use std::prelude::v1::*;
use std::{
    collections::VecDeque,
    ffi::c_void,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
unsafe extern "C" {
    fn macos_scan_authorization() -> i32;
    fn macos_scan_context(arg: *mut c_void) -> *mut c_void;
    fn macos_scan_manager(context: *mut c_void) -> *mut c_void;
    fn macos_scan_status(context: *mut c_void) -> i32;
    fn macos_scan_start(context: *mut c_void) -> bool;
    fn macos_scan_stop(context: *mut c_void) -> bool;
    fn macos_scan_detach(context: *mut c_void);
    fn macos_scan_release(context: *mut c_void);
}
struct Reports {
    manager: usize,
    scanning: bool,
    filter: Option<[u8; 16]>,
    pending: Option<Report>,
    matched: bool,
    rows: VecDeque<Report>,
}
pub(crate) struct State {
    sdk: *mut c_void,
    reports: Arc<Mutex<Reports>>,
    manager: bool,
}
impl State {
    fn ready(&mut self) -> Result<(), Error> {
        let permission = unsafe { macos_scan_authorization() };
        if permission != 0 {
            return status(permission);
        }
        if !self.manager {
            let manager = unsafe { macos_scan_manager(self.sdk) };
            self.reports.lock().unwrap().manager = manager as usize;
            self.manager = true;
        }
        let start = Instant::now();
        loop {
            let value = unsafe { macos_scan_status(self.sdk) };
            if value != 2 {
                return status(value);
            }
            if start.elapsed() >= Duration::from_millis(if cfg!(test) { 50 } else { 5000 }) {
                return Err(Error::Timeout);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn detach(&mut self) {
        unsafe { macos_scan_detach(self.sdk) };
        self.manager = false;
        let mut r = self.reports.lock().unwrap();
        r.scanning = false;
        r.manager = 0;
        r.pending = None;
        r.rows.clear();
    }
}
#[no_mangle]
unsafe extern "C" fn macos_scan_begin_rust(
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
    let mut row = Report {
        peer: [0; 16],
        rssi_dbm: None,
        matches_filter: true,
    };
    unsafe { core::ptr::copy_nonoverlapping(peer, row.peer.as_mut_ptr(), 16) };
    if valid && rssi != 127 && i16::try_from(rssi).is_ok() {
        row.rssi_dbm = Some(rssi as i16);
    }
    r.pending = Some(row);
}
#[no_mangle]
unsafe extern "C" fn macos_scan_uuid_rust(arg: *mut c_void, data: *const u8, length: usize) {
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
unsafe extern "C" fn macos_scan_end_rust(arg: *mut c_void) {
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

fn status(code: i32) -> Result<(), Error> {
    match code {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        2 => Err(Error::Busy),
        3 => Err(Error::Unavailable),
        4 => Err(Error::PermissionDenied),
        5 => Err(Error::InvalidValue),
        6 => Err(Error::Timeout),
        7 => Err(Error::Io),
        _ => Err(Error::Io),
    }
}

// SAFETY: acquired and used exclusively by the private scan worker; callback
// storage remains retained until stop and the SDK detach barrier complete.
unsafe impl Send for State {}
impl ScanBackend for State {
    fn open() -> Result<Self, Error> {
        status(unsafe { macos_scan_authorization() })?;
        let reports = Arc::new(Mutex::new(Reports {
            manager: 0,
            scanning: false,
            filter: None,
            pending: None,
            matched: false,
            rows: VecDeque::with_capacity(32),
        }));
        let sdk = unsafe { macos_scan_context(Arc::as_ptr(&reports).cast_mut().cast()) };
        if sdk.is_null() {
            return Err(Error::Io);
        }
        let mut state = Self {
            sdk,
            reports,
            manager: false,
        };
        if let Err(e) = state.ready() {
            state.detach();
            unsafe {
                macos_scan_release(sdk);
            }
            return Err(e);
        }
        Ok(state)
    }
    fn start(&mut self, service: Option<Uuid>) -> Result<(), Error> {
        self.ready()?;
        {
            let mut r = self.reports.lock().unwrap();
            if r.scanning {
                return Err(Error::Busy);
            }
            r.rows.clear();
            r.filter = service.map(|v| v.0);
            r.scanning = true;
        }
        if unsafe { macos_scan_start(self.sdk) } {
            Ok(())
        } else {
            self.reports.lock().unwrap().scanning = false;
            Err(Error::Io)
        }
    }
    fn get(&mut self, rows: &mut [Report]) -> Result<usize, Error> {
        if !self.reports.lock().unwrap().scanning {
            return Err(Error::Unavailable);
        }
        status(unsafe { macos_scan_status(self.sdk) })?;
        let mut r = self.reports.lock().unwrap();
        let n = rows.len().min(r.rows.len());
        for row in rows.iter_mut().take(n) {
            *row = r.rows.pop_front().unwrap();
        }
        Ok(n)
    }
    fn stop(&mut self) -> Result<(), Error> {
        if !unsafe { macos_scan_stop(self.sdk) } {
            return Err(Error::Io);
        }
        self.detach();
        Ok(())
    }
    fn close(&mut self) {
        self.detach();
        unsafe {
            macos_scan_release(self.sdk);
        }
    }
}

#[cfg(all(test, feature = "probe"))]
#[path = "../../../../../tests/host/radio_shims/ble_macos_scan.rs"]
mod witness;

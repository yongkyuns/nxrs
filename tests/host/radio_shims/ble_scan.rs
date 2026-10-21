// Shared C witness calls into the same typed scan backend as production.
#[repr(C)]
struct RawReport {
    peer: [u8; 16],
    rssi_dbm: i32,
    matches_filter: u32,
}
fn status_code(result: Result<(), Error>) -> i32 {
    match result {
        Ok(()) => 0,
        Err(e) => match e {
            Error::Unsupported => 1,
            Error::Busy => 2,
            Error::Unavailable => 3,
            Error::PermissionDenied => 4,
            Error::InvalidValue => 5,
            Error::Timeout => 6,
            Error::Io => 7,
        },
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_open(out: *mut *mut core::ffi::c_void) -> i32 {
    if out.is_null() {
        return 5;
    }
    unsafe {
        *out = core::ptr::null_mut();
    }
    match State::open() {
        Ok(s) => {
            unsafe {
                *out = std::boxed::Box::into_raw(std::boxed::Box::new(s)).cast();
            }
            0
        }
        Err(e) => status_code(Err(e)),
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_start(
    context: *mut core::ffi::c_void,
    service: *const u8,
) -> i32 {
    if context.is_null() {
        return 5;
    }
    let service = if service.is_null() {
        None
    } else {
        let mut b = [0; 16];
        unsafe {
            core::ptr::copy_nonoverlapping(service, b.as_mut_ptr(), 16);
        }
        Some(Uuid(b))
    };
    status_code(unsafe { &mut *context.cast::<State>() }.start(service))
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_get(
    context: *mut core::ffi::c_void,
    rows: *mut RawReport,
    capacity: u32,
    count: *mut u32,
) -> i32 {
    if context.is_null() || rows.is_null() || capacity == 0 || count.is_null() {
        return 5;
    }
    unsafe {
        *count = 0;
    }
    let mut reports = [Report::default(); 32];
    let size = (capacity as usize).min(reports.len());
    match unsafe { &mut *context.cast::<State>() }.get(&mut reports[..size]) {
        Ok(n) => {
            for (i, r) in reports[..n].iter().enumerate() {
                unsafe {
                    rows.add(i).write(RawReport {
                        peer: r.peer,
                        rssi_dbm: r.rssi_dbm.map_or(i32::MIN, i32::from),
                        matches_filter: u32::from(r.matches_filter),
                    });
                }
            }
            unsafe {
                *count = n as u32;
            }
            0
        }
        Err(e) => status_code(Err(e)),
    }
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_stop(context: *mut core::ffi::c_void) -> i32 {
    if context.is_null() {
        return 5;
    }
    status_code(unsafe { &mut *context.cast::<State>() }.stop())
}
#[no_mangle]
unsafe extern "C" fn nxrs_host_ble_scan_release(context: *mut core::ffi::c_void) {
    if context.is_null() {
        return;
    }
    let state = unsafe { &mut *context.cast::<State>() };
    if state.stop().is_err() {
        return;
    }
    state.close();
    unsafe {
        drop(std::boxed::Box::from_raw(context.cast::<State>()));
    }
}

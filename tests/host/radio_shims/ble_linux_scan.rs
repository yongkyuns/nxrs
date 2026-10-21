use super::*;
use core::ffi::c_void;
include!("ble_scan.rs");
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_pump(context: *mut c_void) {
    unsafe { &*context.cast::<State>() }.events.pump(64);
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_field(context: *mut c_void, field: u32) -> usize {
    let s = unsafe { &*context.cast::<State>() };
    match field {
        0 => usize::from(s.owned),
        1 => usize::from(s.signals.reports.borrow().scanning),
        2 => s.signals.callbacks.get(),
        3 => s.signals.reports.borrow().paths.len(),
        _ => 0,
    }
}

#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_queue(context: *mut c_void, path: *const c_char) {
    unsafe { &*context.cast::<State>() }
        .signals
        .queue(unsafe { gio::borrowed_text(path) });
}

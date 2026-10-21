use super::*;
include!("ble_scan.rs");
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_sdk(context: *mut c_void) -> *mut c_void {
    unsafe { &*context.cast::<State>() }.sdk
}
#[no_mangle]
unsafe extern "C" fn nxrs_ble_scan_test_uuid(
    data: *const u8,
    len: usize,
    service: *const u8,
) -> bool {
    canonical(unsafe { core::slice::from_raw_parts(data, len) })
        .is_some_and(|uuid| uuid == unsafe { core::slice::from_raw_parts(service, 16) })
}

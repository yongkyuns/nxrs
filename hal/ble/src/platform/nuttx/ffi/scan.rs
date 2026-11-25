use super::RawRecord;
use core::ffi::c_int;

unsafe extern "C" {
    pub(super) fn ble_scan_open() -> c_int;
    pub(super) fn ble_scan_check(fd: c_int) -> c_int;
    pub(super) fn ble_scan_start(fd: c_int) -> c_int;
    pub(super) fn ble_scan_get(
        fd: c_int,
        records: *mut RawRecord,
        capacity: u8,
        count: *mut u8,
    ) -> c_int;
    pub(super) fn ble_scan_stop(fd: c_int) -> c_int;

}

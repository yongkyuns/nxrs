use super::Candidate;
use core::ffi::c_void;

unsafe extern "C" {
    pub(super) fn macos_wifi_permission() -> i32;
    pub(super) fn macos_wifi_open(context: *mut *mut c_void) -> i32;
    pub(super) fn macos_wifi_release(object: *mut c_void);
    pub(super) fn macos_wifi_state(
        context: *mut c_void,
        powered: *mut u32,
        mode: *mut u32,
        bssid: *mut u8,
        length: *mut u32,
    ) -> i32;
    pub(super) fn macos_wifi_addresses(
        context: *mut c_void,
        arg: *mut c_void,
        emit: unsafe extern "C" fn(*mut c_void, u32, u32, *const u8),
    ) -> i32;
    pub(super) fn macos_wifi_scan(
        context: *mut c_void,
        ssid: *const u8,
        length: usize,
        snapshot: *mut *mut c_void,
    ) -> i32;
    pub(super) fn macos_wifi_count(snapshot: *mut c_void) -> usize;
    pub(super) fn macos_wifi_row(snapshot: *mut c_void, index: usize, row: *mut Candidate) -> i32;
    pub(super) fn macos_wifi_associate(
        context: *mut c_void,
        snapshot: *mut c_void,
        index: usize,
        password: *const u8,
        length: usize,
    ) -> i32;
    pub(super) fn macos_wifi_disassociate(context: *mut c_void);
}

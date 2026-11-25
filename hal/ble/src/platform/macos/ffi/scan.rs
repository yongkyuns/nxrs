use core::ffi::c_void;

unsafe extern "C" {
    pub(super) fn macos_scan_authorization() -> i32;
    pub(super) fn macos_scan_context(arg: *mut c_void) -> *mut c_void;
    pub(super) fn macos_scan_manager(context: *mut c_void) -> *mut c_void;
    pub(super) fn macos_scan_status(context: *mut c_void) -> i32;
    pub(super) fn macos_scan_start(context: *mut c_void) -> bool;
    pub(super) fn macos_scan_stop(context: *mut c_void) -> bool;
    pub(super) fn macos_scan_detach(context: *mut c_void);
    pub(super) fn macos_scan_release(context: *mut c_void);
}

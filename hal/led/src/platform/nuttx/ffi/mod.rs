use core::ffi::c_int;

unsafe extern "C" {
    // 0 = success; otherwise the positive errno captured by the C helper.
    // The helpers never close/retain fd and never retain output pointers.
    pub(super) fn userled_supported(fd: c_int, supported: *mut u32) -> c_int;
    pub(super) fn userled_set(fd: c_int, index: u8, on: c_int) -> c_int;
}

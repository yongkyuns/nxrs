use core::ffi::c_void;

unsafe extern "C" {
    pub(super) fn getrandom(buffer: *mut c_void, size: usize, flags: u32) -> isize;
    pub(super) fn tls_nonblocking_entropy_flag() -> u32;
}

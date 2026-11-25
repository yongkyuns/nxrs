use core::ffi::{c_int, c_void};

unsafe extern "C" {
    pub(super) fn getentropy(buffer: *mut c_void, size: usize) -> c_int;
}

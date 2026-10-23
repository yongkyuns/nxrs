//! Private native-header ABI of this test fixture only.
use std::io;
unsafe extern "C" {
    fn nxrs_tls_test_resources(values: *mut u64) -> std::ffi::c_int;
}
pub(super) fn read() -> io::Result<[u64; 10]> {
    let mut values = [0; 10];
    // SAFETY: actual target C helper writes ten initialized uint64_t values
    // synchronously; the current thread/TCB and writable array stay alive.
    if unsafe { nxrs_tls_test_resources(values.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(values)
}

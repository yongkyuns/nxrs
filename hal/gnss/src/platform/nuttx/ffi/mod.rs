//! Mechanical target termios controls; no reading, parsing or device policy.
use std::{ffi::c_int, io};
unsafe extern "C" {
    fn gnss_serial_nonblocking_flag() -> c_int;
    fn gnss_serial_capture(fd: c_int, saved: *mut u8) -> c_int;
    fn gnss_serial_configure(fd: c_int, saved: *const u8, baud: u32) -> c_int;
    fn gnss_serial_restore(fd: c_int, saved: *const u8) -> c_int;
}
pub(super) fn nonblocking_flag() -> c_int {
    // SAFETY: returns only the target-header constant, without side effects.
    unsafe { gnss_serial_nonblocking_flag() }
}
fn status(value: c_int) -> io::Result<()> {
    if value == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}
pub(super) fn capture(fd: c_int, saved: &mut [u8; 64]) -> io::Result<()> {
    // SAFETY: exact bounded output buffer; native code retains nothing.
    status(unsafe { gnss_serial_capture(fd, saved.as_mut_ptr()) })
}
pub(super) fn configure(fd: c_int, saved: &[u8; 64], baud: u32) -> io::Result<()> {
    // SAFETY: buffer was filled by capture for this live descriptor.
    status(unsafe { gnss_serial_configure(fd, saved.as_ptr(), baud) })
}
pub(super) fn restore(fd: c_int, saved: &[u8; 64]) -> io::Result<()> {
    // SAFETY: same live descriptor and saved target-header representation.
    status(unsafe { gnss_serial_restore(fd, saved.as_ptr()) })
}

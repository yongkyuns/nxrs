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

/// Own newly created descriptors immediately; native code retains neither.
pub(super) fn event_pipe() -> io::Result<(std::fs::File, std::fs::File)> {
    use std::os::fd::FromRawFd;
    let mut descriptors = [-1; 2];
    // SAFETY: two writable int slots for this synchronous pipe2 call.
    if unsafe { gnss_event_pipe(descriptors.as_mut_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: pipe2 transferred two fresh, distinct owned descriptors.
    Ok(unsafe {
        (
            std::fs::File::from_raw_fd(descriptors[0]),
            std::fs::File::from_raw_fd(descriptors[1]),
        )
    })
}
pub(super) fn wait(device: c_int, cancel: c_int) -> io::Result<bool> {
    // SAFETY: descriptors remain borrowed/alive through the synchronous poll.
    match unsafe { gnss_event_wait(device, cancel) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn gnss_event_pipe(descriptors: *mut c_int) -> c_int;
    fn gnss_event_wait(device: c_int, cancel: c_int) -> c_int;
}

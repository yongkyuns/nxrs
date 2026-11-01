unsafe extern "C" {
    fn console_ready(fd: i32) -> i32;
    fn console_mode(fd: i32, fields: *mut u32, raw: i32) -> i32;
}
pub(crate) fn ready(fd: i32) -> io::Result<bool> {
    // SAFETY: target poll translation only observes this owner's live fd.
    match unsafe { console_ready(fd) } {
        n if n < 0 => Err(io::Error::last_os_error()),
        n => Ok(n > 0),
    }
}
pub(crate) fn mode(fd: i32, saved: &mut [u32; 6], raw: bool) -> io::Result<()> {
    // SAFETY: six live scalar fields match the private target-header ABI.
    if unsafe { console_mode(fd, saved.as_mut_ptr(), i32::from(raw)) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}
use std::io;

//! Target poll/pipe ABI. Borrowed descriptors stay alive through each call.
use std::{ffi::c_int, io};
/// Own newly created descriptors immediately; native code retains neither.
pub fn pipe() -> io::Result<(std::fs::File, std::fs::File)> {
    use std::os::fd::FromRawFd;
    let mut descriptors = [-1; 2];
    // SAFETY: two writable int slots for this synchronous pipe2 call.
    if unsafe { acquisition_event_pipe(descriptors.as_mut_ptr()) } < 0 {
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
pub fn wait(device: c_int, cancel: c_int) -> io::Result<bool> {
    // SAFETY: descriptors remain borrowed/alive through the synchronous poll.
    match unsafe { acquisition_event_wait(device, cancel) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn acquisition_event_pipe(descriptors: *mut c_int) -> c_int;
    fn acquisition_event_wait(device: c_int, cancel: c_int) -> c_int;
}

pub fn tick(cancel: c_int, timeout_ms: c_int) -> io::Result<bool> {
    // SAFETY: borrowed live descriptor and a bounded poll timeout, no retained data.
    match unsafe { acquisition_event_tick(cancel, timeout_ms) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn acquisition_event_tick(cancel: c_int, timeout_ms: c_int) -> c_int;
}

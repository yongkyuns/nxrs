use crate::Error;
use std::os::fd::{AsRawFd, BorrowedFd};

unsafe extern "C" {
    fn pwm_configure(fd: i32, frequency: u32, duty: u16) -> i32;
    fn pwm_start_output(fd: i32) -> i32;
    fn pwm_stop_output(fd: i32) -> i32;
}

pub(super) fn configure(fd: BorrowedFd<'_>, frequency: u32, duty: u16) -> Result<(), Error> {
    // SAFETY: live descriptor and scalar settings for a synchronous ioctl.
    match unsafe { pwm_configure(fd.as_raw_fd(), frequency, duty) } {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        _ => Err(Error::Io),
    }
}

pub(super) fn start(fd: BorrowedFd<'_>) -> Result<(), Error> {
    // SAFETY: live descriptor and synchronous ioctl.
    (unsafe { pwm_start_output(fd.as_raw_fd()) } == 0)
        .then_some(())
        .ok_or(Error::Io)
}

pub(super) fn stop(fd: BorrowedFd<'_>) -> Result<(), Error> {
    // SAFETY: live descriptor and synchronous ioctl.
    (unsafe { pwm_stop_output(fd.as_raw_fd()) } == 0)
        .then_some(())
        .ok_or(Error::Io)
}

use std::{
    io,
    os::fd::{AsRawFd, BorrowedFd},
};

unsafe extern "C" {
    fn adc_nonblocking_flag() -> i32;
    fn adc_reset_fifo(fd: i32) -> i32;
    fn adc_trigger(fd: i32) -> i32;
}

pub(super) fn nonblocking() -> i32 {
    // SAFETY: returns a constant from the target headers.
    unsafe { adc_nonblocking_flag() }
}

pub(super) fn reset_fifo(fd: BorrowedFd<'_>) -> io::Result<()> {
    // SAFETY: live descriptor and synchronous ioctl.
    status(unsafe { adc_reset_fifo(fd.as_raw_fd()) })
}

pub(super) fn trigger(fd: BorrowedFd<'_>) -> io::Result<()> {
    // SAFETY: live descriptor and synchronous ioctl.
    status(unsafe { adc_trigger(fd.as_raw_fd()) })
}

fn status(result: i32) -> io::Result<()> {
    if result == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

use crate::Error;
use std::os::fd::{AsRawFd, BorrowedFd};

unsafe extern "C" {
    fn rtc_is_set(fd: i32, value: *mut bool) -> i32;
    fn rtc_read_seconds(fd: i32, seconds: *mut i64) -> i32;
    fn rtc_set_seconds(fd: i32, seconds: i64) -> i32;
}

pub(super) fn is_set(fd: BorrowedFd<'_>) -> Result<bool, Error> {
    let mut value = false;
    // SAFETY: the descriptor and output pointer remain valid for this ioctl.
    result(unsafe { rtc_is_set(fd.as_raw_fd(), &mut value) })?;
    Ok(value)
}

pub(super) fn read_seconds(fd: BorrowedFd<'_>) -> Result<i64, Error> {
    let mut seconds = 0;
    // SAFETY: the descriptor and output pointer remain valid for this ioctl.
    result(unsafe { rtc_read_seconds(fd.as_raw_fd(), &mut seconds) })?;
    Ok(seconds)
}

pub(super) fn set_seconds(fd: BorrowedFd<'_>, seconds: i64) -> Result<(), Error> {
    // SAFETY: the descriptor stays open during the synchronous call.
    result(unsafe { rtc_set_seconds(fd.as_raw_fd(), seconds) })
}

fn result(status: i32) -> Result<(), Error> {
    match status {
        0 => Ok(()),
        1 => Err(Error::Unsupported),
        2 => Err(Error::InvalidTime),
        _ => Err(Error::Io),
    }
}

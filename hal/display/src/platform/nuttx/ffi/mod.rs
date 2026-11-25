use crate::Error;
use std::{
    ffi::c_int,
    os::fd::{AsRawFd, BorrowedFd},
};

unsafe extern "C" {
    fn display_spi_write(
        fd: c_int,
        frequency: u32,
        command: u8,
        data: *const u8,
        len: usize,
    ) -> c_int;
}

pub(super) fn write(
    fd: BorrowedFd<'_>,
    frequency: u32,
    command: u8,
    data: &[u8],
) -> Result<(), Error> {
    // SAFETY: the descriptor and buffer remain valid through the synchronous ioctl.
    let result = unsafe {
        display_spi_write(
            fd.as_raw_fd(),
            frequency,
            command,
            data.as_ptr(),
            data.len(),
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(Error::Io)
    }
}

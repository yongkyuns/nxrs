use crate::Error;
use std::{
    ffi::c_int,
    os::fd::{AsRawFd, BorrowedFd},
};

pub(super) fn panel_bytes(pixels: &mut [u16]) -> &[u8] {
    for pixel in pixels.iter_mut() {
        *pixel = pixel.to_be();
    }
    // SAFETY: u16 has no padding and every bit pattern is valid for u8.
    // The borrowed byte view covers exactly the initialized pixel storage.
    unsafe { std::slice::from_raw_parts(pixels.as_ptr().cast(), std::mem::size_of_val(pixels)) }
}

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

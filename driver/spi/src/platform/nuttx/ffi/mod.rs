use std::{ffi::c_int, io};
unsafe extern "C" {
    fn spi_resource_transfer(
        fd: c_int,
        select: u16,
        frequency: u32,
        mode: u8,
        tx: *const u8,
        rx: *mut u8,
        len: usize,
    ) -> c_int;
}
pub(super) fn transfer(
    fd: c_int,
    select: u16,
    frequency: u32,
    mode: u8,
    tx: &[u8],
    rx: &mut [u8],
) -> io::Result<()> {
    debug_assert_eq!(tx.len(), rx.len());
    // SAFETY: live descriptor and equal, bounded buffers borrowed for one
    // synchronous ioctl. The native helper retains neither pointers nor fd.
    let result = unsafe {
        spi_resource_transfer(
            fd,
            select,
            frequency,
            mode,
            tx.as_ptr(),
            rx.as_mut_ptr(),
            tx.len(),
        )
    };
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

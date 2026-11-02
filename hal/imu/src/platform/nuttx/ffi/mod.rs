//! Target-header atomic I2C transfer layout only; no sensor commands or conversion.
use std::{ffi::c_int, io};
unsafe extern "C" {
    fn imu_i2c_transfer(
        fd: c_int,
        address: u8,
        frequency: u32,
        write: *const u8,
        write_len: usize,
        read: *mut u8,
        read_len: usize,
    ) -> c_int;
}
pub(super) fn transfer(
    fd: c_int,
    address: u8,
    frequency: u32,
    write: &[u8],
    read: &mut [u8],
) -> io::Result<()> {
    // SAFETY: live borrowed descriptor and bounded slices for one synchronous
    // SDK ioctl. The helper never closes/retains descriptors or buffers.
    let result = unsafe {
        imu_i2c_transfer(
            fd,
            address,
            frequency,
            write.as_ptr(),
            write.len(),
            read.as_mut_ptr(),
            read.len(),
        )
    };
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

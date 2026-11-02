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

/// Own newly created descriptors immediately; native code retains neither.
pub(super) fn event_pipe() -> io::Result<(std::fs::File, std::fs::File)> {
    use std::os::fd::FromRawFd;
    let mut descriptors = [-1; 2];
    // SAFETY: two writable int slots for this synchronous pipe2 call.
    if unsafe { imu_event_pipe(descriptors.as_mut_ptr()) } < 0 {
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
    match unsafe { imu_event_wait(device, cancel) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn imu_event_pipe(descriptors: *mut c_int) -> c_int;
    fn imu_event_wait(device: c_int, cancel: c_int) -> c_int;
}

pub(super) fn irq_register(fd: c_int) -> io::Result<()> {
    // SAFETY: live borrowed GPIO descriptor, IRQ context remains driver-owned.
    if unsafe { imu_irq_register(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
pub(super) fn irq_unregister(fd: c_int) -> io::Result<()> {
    // SAFETY: live borrowed GPIO descriptor; no Rust callback/context registered.
    if unsafe { imu_irq_unregister(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
pub(super) fn irq_read(fd: c_int) -> io::Result<bool> {
    // SAFETY: live GPIO descriptor; native code retains no pointers.
    match unsafe { imu_irq_read(fd) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn imu_irq_register(fd: c_int) -> c_int;
    fn imu_irq_unregister(fd: c_int) -> c_int;
    fn imu_irq_read(fd: c_int) -> c_int;
}

pub(super) fn irq_check(fd: c_int) -> io::Result<()> {
    // SAFETY: live GPIO descriptor; checks native pin configuration without mutation.
    if unsafe { imu_irq_check(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
unsafe extern "C" {
    fn imu_irq_check(fd: c_int) -> c_int;
}

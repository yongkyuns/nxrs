//! Target GPIO readiness and cancellation ABI; sensor lifecycle stays in Rust.
use std::{ffi::c_int, io};
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

pub(super) fn tick(cancel: c_int, timeout_ms: c_int) -> io::Result<bool> {
    // SAFETY: borrowed live descriptor and a bounded poll timeout, no retained data.
    match unsafe { imu_event_tick(cancel, timeout_ms) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn imu_event_tick(cancel: c_int, timeout_ms: c_int) -> c_int;
}

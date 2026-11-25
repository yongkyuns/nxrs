//! Target GPIO ioctl layout only; no Rust callbacks are registered with NuttX.
use std::{ffi::c_int, io};
pub(super) fn irq_timestamp(fd: c_int) -> io::Result<Option<(usize, u64)>> {
    let mut time = 0;
    let mut sequence = 0;
    // SAFETY: live descriptor and two writable output slots for a synchronous ioctl.
    match unsafe { gpio_resource_irq_timestamp(fd, &mut time, &mut sequence) } {
        0 => Ok(None),
        1 => Ok(Some((sequence, time))),
        _ => Err(io::Error::last_os_error()),
    }
}

unsafe extern "C" {
    fn gpio_resource_irq_timestamp(fd: c_int, time: *mut u64, sequence: *mut usize) -> c_int;
}
pub(super) fn irq_register(fd: c_int) -> io::Result<()> {
    // SAFETY: live borrowed GPIO descriptor, IRQ context remains driver-owned.
    if unsafe { gpio_resource_irq_register(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
pub(super) fn irq_unregister(fd: c_int) -> io::Result<()> {
    // SAFETY: live borrowed GPIO descriptor; no Rust callback/context registered.
    if unsafe { gpio_resource_irq_unregister(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
pub(super) fn irq_read(fd: c_int) -> io::Result<bool> {
    // SAFETY: live GPIO descriptor; native code retains no pointers.
    match unsafe { gpio_resource_irq_read(fd) } {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(io::Error::last_os_error()),
    }
}
unsafe extern "C" {
    fn gpio_resource_irq_register(fd: c_int) -> c_int;
    fn gpio_resource_irq_unregister(fd: c_int) -> c_int;
    fn gpio_resource_irq_read(fd: c_int) -> c_int;
}

pub(super) fn irq_check(fd: c_int) -> io::Result<()> {
    // SAFETY: live GPIO descriptor; checks native pin configuration without mutation.
    if unsafe { gpio_resource_irq_check(fd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
unsafe extern "C" {
    fn gpio_resource_irq_check(fd: c_int) -> c_int;
}

pub(super) fn check_pin(fd: c_int, output: bool) -> io::Result<()> {
    // SAFETY: the descriptor remains open during the synchronous ioctl.
    if unsafe { gpio_check_pin(fd, output) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

unsafe extern "C" {
    fn gpio_check_pin(fd: c_int, output: bool) -> c_int;
}

//! Target GPIO ioctl layout only; no Rust callbacks are registered with NuttX.
use std::{ffi::c_int, io};
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

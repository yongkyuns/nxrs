//! Mechanical target-header ioctls; ownership and error policy stay in Rust.
use crate::DeviceError;

unsafe extern "C" {
    fn watchdog_reset_status(fd: i32, active: *mut bool, reset: *mut bool) -> i32;
    fn watchdog_disarm(fd: i32) -> i32;
    fn watchdog_set_timeout(fd: i32, ms: u32) -> i32;
    fn watchdog_start(fd: i32) -> i32;
    fn watchdog_feed(fd: i32) -> i32;
}

pub(super) fn status(fd: i32) -> Result<(bool, bool), DeviceError> {
    let (mut active, mut reset) = (false, false);
    // SAFETY: live owned descriptor; two initialized writable C bool outputs.
    if unsafe { watchdog_reset_status(fd, &mut active, &mut reset) } < 0 {
        return Err(DeviceError::Io);
    }
    Ok((active, reset))
}

pub(super) fn disarm(fd: i32) -> Result<(), DeviceError> {
    // SAFETY: exclusive owner, synchronous ioctl with no retained pointers.
    if unsafe { watchdog_disarm(fd) } < 0 {
        return Err(DeviceError::Io);
    }
    Ok(())
}

pub(super) fn set_timeout(fd: i32, ms: u32) -> Result<(), DeviceError> {
    // SAFETY: live exclusive descriptor and a scalar timeout.
    match unsafe { watchdog_set_timeout(fd, ms) } {
        0 => Ok(()),
        1 => Err(DeviceError::InvalidTimeout),
        _ => Err(DeviceError::Io),
    }
}

pub(super) fn start(fd: i32) -> Result<(), DeviceError> {
    // SAFETY: live exclusive descriptor; ioctl retains nothing.
    if unsafe { watchdog_start(fd) } < 0 {
        return Err(DeviceError::Io);
    }
    Ok(())
}

pub(super) fn feed(fd: i32) -> Result<(), DeviceError> {
    // SAFETY: live exclusive descriptor; ioctl retains nothing.
    if unsafe { watchdog_feed(fd) } < 0 {
        return Err(DeviceError::Io);
    }
    Ok(())
}

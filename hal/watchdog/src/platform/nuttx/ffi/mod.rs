//! Mechanical target-header ioctls; ownership and error policy stay in Rust.
use crate::DeviceError;

unsafe extern "C" {
    fn watchdog_reset_status(fd: i32, active: *mut bool, reset: *mut bool) -> i32;
    fn watchdog_disarm(fd: i32) -> i32;
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

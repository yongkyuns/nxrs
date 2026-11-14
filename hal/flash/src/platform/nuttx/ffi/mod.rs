use crate::Error;
use std::os::fd::{AsRawFd, BorrowedFd};

unsafe extern "C" {
    fn flash_direct_flags() -> i32;
    fn flash_geometry(fd: i32, program: *mut u32, erase: *mut u32, sectors: *mut u32) -> i32;
    fn flash_erased_byte(fd: i32, value: *mut u8) -> i32;
    fn flash_erase_sectors(fd: i32, start: u32, count: u32) -> i32;
    fn flash_discard_cache(fd: i32) -> i32;
}

pub(super) fn direct_flags() -> i32 {
    // SAFETY: returns target constants without accessing any resource.
    unsafe { flash_direct_flags() }
}

pub(super) fn geometry(fd: BorrowedFd<'_>) -> Result<(u32, u32, u32), Error> {
    let (mut program, mut erase, mut sectors) = (0, 0, 0);
    // SAFETY: live descriptor and three valid scalar outputs.
    if unsafe { flash_geometry(fd.as_raw_fd(), &mut program, &mut erase, &mut sectors) } != 0 {
        return Err(Error::Io);
    }
    Ok((program, erase, sectors))
}

pub(super) fn erased_byte(fd: BorrowedFd<'_>) -> Result<u8, Error> {
    let mut value = 0;
    // SAFETY: live descriptor and one valid scalar output.
    if unsafe { flash_erased_byte(fd.as_raw_fd(), &mut value) } != 0 {
        return Err(Error::Io);
    }
    Ok(value)
}

pub(super) fn erase(fd: BorrowedFd<'_>, start: u32, count: u32) -> Result<(), Error> {
    // SAFETY: live descriptor and range already checked against its geometry.
    match unsafe { flash_erase_sectors(fd.as_raw_fd(), start, count) } {
        0 => Ok(()),
        n if n > 0 && n as u32 == count => Ok(()),
        _ => Err(Error::Io),
    }
}

pub(super) fn discard(fd: BorrowedFd<'_>) -> Result<(), Error> {
    // SAFETY: live descriptor; the ioctl retains nothing.
    if unsafe { flash_discard_cache(fd.as_raw_fd()) } != 0 {
        return Err(Error::Io);
    }
    Ok(())
}

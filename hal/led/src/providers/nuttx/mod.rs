//! NuttX USERLED implementation: std owns the file, private FFI handles controls.
//! See the capability README for target C integration and qualification requirements.
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", all(test, unix))))]
compile_error!("NuttX LED provider requires NuttX; host admission is unit-test-only");

use crate::{DeviceError, Led, LedSet};
use std::ffi::c_int;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;

unsafe extern "C" {
    // 0 = success; otherwise the positive errno captured by the C helper.
    // The helpers never close/retain fd and never retain output pointers.
    fn nxrs_userled_supported(fd: c_int, supported: *mut u32) -> c_int;
    fn nxrs_userled_set(fd: c_int, index: u8, on: c_int) -> c_int;
}

pub struct UserLeds {
    file: File,
    supported: LedSet,
}

impl UserLeds {
    /// Resource binding is provider-local. The build may override the default
    /// through NXRS_USERLED_PATH; portable callers never supply a device path.
    #[cfg_attr(all(test, not(nxrs_provider = "nuttx")), allow(dead_code))]
    pub fn open() -> Result<Self, DeviceError> {
        Self::open_path(option_env!("NXRS_USERLED_PATH").unwrap_or("/dev/userleds"))
    }

    fn open_path(path: &str) -> Result<Self, DeviceError> {
        // Do not create or truncate device nodes. Failed configuration drops
        // this same File, without adopting its descriptor into a second owner.
        let file = OpenOptions::new()
            .write(true)
            .open(path)
            .map_err(device_error)?;
        let mut supported = 0u32;
        // SAFETY: live borrowed descriptor and valid aligned output. The helper
        // uses target headers to construct the native ioctl argument.
        status(unsafe { nxrs_userled_supported(file.as_raw_fd(), &mut supported) })?;
        Ok(Self {
            file,
            supported: LedSet::from_bits(supported),
        })
    }
}

impl Led for UserLeds {
    fn supported(&self) -> LedSet {
        self.supported
    }

    fn set(&mut self, index: u8, on: bool) -> Result<(), DeviceError> {
        self.supported.validate(index)?;
        // SAFETY: this provider retains ownership throughout the synchronous
        // call. index is supported, and the helper retains no pointer/fd.
        status(unsafe { nxrs_userled_set(self.file.as_raw_fd(), index, if on { 1 } else { 0 }) })
    }
}

fn status(error: c_int) -> Result<(), DeviceError> {
    match error {
        0 => Ok(()),
        n if n > 0 => Err(device_error(io::Error::from_raw_os_error(n))),
        _ => Err(DeviceError::Io), // Reject a broken helper contract.
    }
}

fn device_error(error: io::Error) -> DeviceError {
    match error.kind() {
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted => DeviceError::Busy,
        io::ErrorKind::TimedOut => DeviceError::Timeout,
        io::ErrorKind::Unsupported => DeviceError::Unsupported,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

#[cfg(test)]
mod tests;

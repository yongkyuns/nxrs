//! Shared NuttX descriptor ownership and bridge error translation only.
#![no_std]
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
compile_error!("NuttX descriptor support requires NuttX or an explicit core-only target fixture");
use core::ffi::c_int;
use hal_common::DeviceError;
extern "C" {
    fn rc_nx_close(fd: c_int) -> c_int;
}

// Stable bridge status codes, not platform errno numbers. Native C structures
// (off_t, timespec, sockaddr, driver ioctl data) never cross this Rust ABI.
pub fn error(code: c_int) -> DeviceError {
    match code {
        -1 => DeviceError::Unsupported,
        -2 => DeviceError::Busy,
        -3 => DeviceError::Timeout,
        -4 => DeviceError::Full,
        -5 => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

pub fn status(code: c_int) -> Result<(), DeviceError> {
    if code == 0 {
        Ok(())
    } else {
        Err(error(code))
    }
}

pub struct OwnedFd {
    descriptor: c_int,
    close_error: Option<DeviceError>,
}

impl OwnedFd {
    /// Adopt the descriptor returned by a successful NuttX open operation.
    ///
    /// # Safety
    /// A nonnegative descriptor must be live and uniquely transferred to this
    /// object. The caller must not close or create another owner for it.
    pub unsafe fn from_raw(fd: c_int) -> Result<Self, DeviceError> {
        if fd >= 0 {
            Ok(Self {
                descriptor: fd,
                close_error: None,
            })
        } else {
            Err(error(fd))
        }
    }

    pub fn raw(&self) -> Result<c_int, DeviceError> {
        if self.descriptor >= 0 {
            Ok(self.descriptor)
        } else {
            Err(self.close_error.unwrap_or(DeviceError::Io))
        }
    }

    pub fn close(&mut self) -> Result<(), DeviceError> {
        if self.descriptor >= 0 {
            // NuttX fdlist_close uninstalls the descriptor BEFORE file_put
            // reports a driver close error. Never retain/retry that number:
            // another device may already have reused it. Consume before FFI.
            let fd = core::mem::replace(&mut self.descriptor, -1);
            // SAFETY: this is the sole close attempt for our owned descriptor.
            let result = status(unsafe { rc_nx_close(fd) });
            self.close_error = result.err();
            result
        } else {
            // A terminal cleanup error remains visible to the service owner;
            // retry/drop cannot turn it into success or repeat the syscall.
            self.close_error.map_or(Ok(()), Err)
        }
    }
}

impl Drop for OwnedFd {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

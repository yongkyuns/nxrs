//! Shared NuttX descriptor ownership; capabilities translate bridge status codes.
#![no_std]
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
compile_error!("NuttX descriptor support requires NuttX or an explicit core-only target fixture");
use core::ffi::c_int;
extern "C" {
    fn rc_nx_close(fd: c_int) -> c_int;
}

// Stable bridge status codes, not platform errno numbers. Translation into
// public API errors belongs to each capability's private provider.
pub fn status(code: c_int) -> Result<(), c_int> {
    if code == 0 {
        Ok(())
    } else {
        Err(code)
    }
}

pub struct OwnedFd {
    descriptor: c_int,
    close_error: Option<c_int>,
}

impl OwnedFd {
    /// Adopt the descriptor returned by a successful NuttX open operation.
    ///
    /// # Safety
    /// A nonnegative descriptor must be live and uniquely transferred to this
    /// object. The caller must not close or create another owner for it.
    pub unsafe fn from_raw(fd: c_int) -> Result<Self, c_int> {
        if fd >= 0 {
            Ok(Self {
                descriptor: fd,
                close_error: None,
            })
        } else {
            Err(fd)
        }
    }

    pub fn raw(&self) -> Result<c_int, c_int> {
        if self.descriptor >= 0 {
            Ok(self.descriptor)
        } else {
            Err(self.close_error.unwrap_or(-6))
        }
    }

    pub fn close(&mut self) -> Result<(), c_int> {
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

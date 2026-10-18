//! NuttX frame-record sink for bounded fixtures; C uses configured target headers.
#![deny(unsafe_op_in_unsafe_fn)]
#[cfg(not(any(target_os = "nuttx", target_os = "none")))]
compile_error!("NuttX storage requires NuttX or an explicit core-only target fixture");
use crate::record::encode_header;
use crate::{DeviceError, Frame, Storage};
use core::ffi::{c_char, c_int, CStr};
use nuttx_support::{status, OwnedFd};
extern "C" {
    fn rc_nx_file_create(path: *const c_char) -> c_int;
    fn rc_nx_append(
        fd: c_int,
        header: *const u8,
        header_len: usize,
        bytes: *const u8,
        len: usize,
    ) -> c_int;
    fn rc_nx_flush(fd: c_int) -> c_int;
}

/// Synchronous VFS record sink for bounded simulator fixtures. Production
/// blocking filesystems need a worker adapter; this does not promise deadlines.
/// Err rolls back the append; failed rollback poisons the sink until disposal.
/// No crash/power-loss atomicity or concurrent-writer guarantee is claimed.
pub struct FileStorage {
    fd: OwnedFd,
    max_records: u64,
    max_frame: usize,
    accepted: u64,
    poisoned: bool,
}

impl FileStorage {
    pub fn create(path: &CStr, max_records: u64, max_frame: usize) -> Result<Self, DeviceError> {
        if max_records == 0 || max_frame == 0 {
            return Err(DeviceError::InvalidData);
        }
        // SAFETY: valid C string; bridge uses O_EXCL, never overwrites a file.
        let fd = unsafe { OwnedFd::from_raw(rc_nx_file_create(path.as_ptr())) }?;
        Ok(Self {
            fd,
            max_records,
            max_frame,
            accepted: 0,
            poisoned: false,
        })
    }
}

impl Storage for FileStorage {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError> {
        if self.poisoned {
            return Err(DeviceError::Io);
        }
        if frame.bytes.len() > self.max_frame
            || frame.capture.len != frame.bytes.len()
            || !frame.capture.format.accepts_len(frame.bytes.len())
        {
            return Err(DeviceError::InvalidData);
        }
        if self.accepted == self.max_records {
            return Err(DeviceError::Full);
        }
        // Same documented RCAMREC1 interchange format as native recordings.
        let header = encode_header(frame.capture, frame.sequence, frame.bytes.len());
        // SAFETY: both byte slices remain alive and immutable during append.
        let rc = unsafe {
            rc_nx_append(
                self.fd.raw()?,
                header.as_ptr(),
                header.len(),
                frame.bytes.as_ptr(),
                frame.bytes.len(),
            )
        };
        if rc == -7 {
            self.poisoned = true;
        }
        status(rc)?;
        self.accepted += 1;
        Ok(())
    }

    fn flush(&mut self) -> Result<(), DeviceError> {
        if self.poisoned {
            return Err(DeviceError::Io);
        }
        // SAFETY: uniquely owned valid file descriptor.
        status(unsafe { rc_nx_flush(self.fd.raw()?) })
    }
}

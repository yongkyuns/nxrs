//! GPIO registration ownership. Notification/callback storage stays in NuttX.
use crate::DeviceError;
use core::sync::atomic::Ordering;
use std::{fs::File, os::fd::AsRawFd};

pub(super) struct Irq {
    pub(super) file: File,
    registered: bool,
}
impl Irq {
    pub(super) fn register(file: File) -> Result<Self, DeviceError> {
        super::ffi::irq_check(file.as_raw_fd()).map_err(|_| DeviceError::Unsupported)?;
        if super::ffi::irq_register(file.as_raw_fd()).is_err() {
            // NuttX increments register_count before attach/enable may fail.
            if super::ffi::irq_unregister(file.as_raw_fd()).is_err() {
                super::OWNED.store(2, Ordering::Release);
            }
            return Err(DeviceError::Io);
        }
        Ok(Self {
            file,
            registered: true,
        })
    }
    pub(super) fn read(&self) -> Result<bool, DeviceError> {
        super::ffi::irq_read(self.file.as_raw_fd()).map_err(|_| DeviceError::Io)
    }
    pub(super) fn unregister(&mut self) -> Result<(), DeviceError> {
        if !core::mem::take(&mut self.registered) {
            return Ok(());
        }
        super::ffi::irq_unregister(self.file.as_raw_fd()).map_err(|_| {
            super::OWNED.store(2, Ordering::Release);
            DeviceError::Io
        })
    }
}
impl Drop for Irq {
    fn drop(&mut self) {
        let _ = self.unregister();
    }
}

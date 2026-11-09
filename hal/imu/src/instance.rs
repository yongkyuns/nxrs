//! Per-device ownership shared by factories; no platform or sensor selection.
use crate::DeviceError;
use std::sync::atomic::{AtomicU8, Ordering};

pub trait Sealed {}

pub(crate) struct Ownership(pub(crate) AtomicU8);
impl Ownership {
    pub(crate) const fn new() -> Self {
        Self(AtomicU8::new(0))
    }
    pub(crate) fn take(&'static self) -> Result<Lease, DeviceError> {
        self.0
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|state| {
                if state == 1 {
                    DeviceError::Busy
                } else {
                    DeviceError::Io
                }
            })?;
        Ok(Lease(self))
    }
    #[allow(dead_code, reason = "Only physical providers quarantine hardware.")]
    pub(crate) fn quarantine(&self) {
        self.0.store(2, Ordering::Release);
    }
}
pub(crate) struct Lease(&'static Ownership);
impl Lease {
    #[allow(dead_code, reason = "Only physical providers quarantine hardware.")]
    pub(crate) fn quarantine(&self) {
        self.0.quarantine();
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = self
            .0
             .0
            .compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}

//! Exclusive device use; failed cleanup keeps the device unavailable until restart.
use crate::DeviceError;
use std::sync::atomic::{AtomicU8, Ordering};

#[repr(u8)]
pub(super) enum State {
    Available,
    InUse,
    Faulted,
}

pub(crate) struct DeviceState {
    pub(super) value: AtomicU8,
}
impl DeviceState {
    pub(crate) const fn new() -> Self {
        Self {
            value: AtomicU8::new(State::Available as u8),
        }
    }
    pub(crate) fn begin_use(&'static self) -> Result<DeviceUseGuard, DeviceError> {
        self.value
            .compare_exchange(
                State::Available as u8,
                State::InUse as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .map_err(|state| {
                if state == State::InUse as u8 {
                    DeviceError::Busy
                } else {
                    DeviceError::Io
                }
            })?;
        Ok(DeviceUseGuard { state: self })
    }
    #[allow(
        dead_code,
        reason = "Only physical providers can fail hardware cleanup."
    )]
    pub(crate) fn mark_faulted(&self) {
        self.value.store(State::Faulted as u8, Ordering::Release);
    }
}

/// Dropped after cleanup; a faulted device must never become available here.
pub(crate) struct DeviceUseGuard {
    state: &'static DeviceState,
}
impl DeviceUseGuard {
    #[allow(
        dead_code,
        reason = "Only physical providers can fail hardware cleanup."
    )]
    pub(crate) fn mark_faulted(&self) {
        self.state.mark_faulted();
    }
}
impl Drop for DeviceUseGuard {
    fn drop(&mut self) {
        let _ = self.state.value.compare_exchange(
            State::InUse as u8,
            State::Available as u8,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
    }
}

use crate::{
    control::{Backend, Owner},
    Control, DeviceError, State,
};
use core::cell::Cell;
use core::marker::PhantomData;
use core::sync::atomic::{AtomicBool, Ordering};
#[allow(unsafe_code)]
mod ffi;

static TAKEN: AtomicBool = AtomicBool::new(false);
struct Provider {
    _not_sync: PhantomData<Cell<()>>,
}
impl Drop for Provider {
    fn drop(&mut self) {
        TAKEN.store(false, Ordering::Release);
    }
}
impl Backend for Provider {
    fn reboot(&mut self) -> Result<core::convert::Infallible, DeviceError> {
        ffi::reboot()
    }
    type Slot = ffi::Secondary;
    fn secondary(&mut self) -> Result<Self::Slot, DeviceError> {
        ffi::Secondary::open()
    }
    fn state(&mut self) -> Result<State, DeviceError> {
        ffi::state()
    }
    fn request_trial(&mut self) -> Result<(), DeviceError> {
        ffi::request_trial()
    }
    fn confirm(&mut self) -> Result<(), DeviceError> {
        ffi::confirm()
    }
}
pub fn open() -> Result<impl Control, DeviceError> {
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(DeviceError::Busy);
    }
    Owner::new(Provider {
        _not_sync: PhantomData,
    })
}

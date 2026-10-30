//! Direct synchronous MCUboot SDK bindings. The SDK opens/closes its native
//! flash resources within each call; one Rust owner excludes concurrent calls.
use crate::{DeviceError, State};
use core::ffi::c_int;

unsafe extern "C" {
    fn boot_swap_type_multi(image: c_int) -> c_int;
    fn boot_set_pending_multi(image: c_int, permanent: c_int) -> c_int;
    fn boot_set_confirmed_multi(image: c_int) -> c_int;
}

// Public MCUboot enum values, checked against target headers by the SDK witness.
pub(super) fn state() -> Result<State, DeviceError> {
    // SAFETY: the provider holds the exclusive SDK lease; image zero is selected
    // by the single-image product configuration. There are no retained callbacks.
    match unsafe { boot_swap_type_multi(0) } {
        1 => Ok(State::Stable),
        2 => Ok(State::Pending),
        4 => Ok(State::Trial),
        value if value < 0 => Err(DeviceError::Io),
        _ => Err(DeviceError::InvalidState),
    }
}
pub(super) fn request_trial() -> Result<(), DeviceError> {
    // SAFETY: exclusive synchronous SDK operation; permanent confirmation is off.
    result(unsafe { boot_set_pending_multi(0, 0) })
}
pub(super) fn confirm() -> Result<(), DeviceError> {
    // SAFETY: exclusive synchronous SDK operation; the Rust controller admits
    // this only for an executing trial image, after caller-established health.
    result(unsafe { boot_set_confirmed_multi(0) })
}
fn result(value: c_int) -> Result<(), DeviceError> {
    if value == 0 {
        Ok(())
    } else {
        Err(DeviceError::Io)
    }
}

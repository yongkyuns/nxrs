//! Trial and confirmation control. Image authentication and recovery belong to
//! the bootloader; application health must be established before confirmation.
#![no_std]
#![deny(unsafe_code)]
extern crate std;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    /// The executing image will be retained on reset.
    Stable,
    /// A staged image is requested for a trial on reset.
    Pending,
    /// The executing trial will revert on reset unless confirmed.
    Trial,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Io,
    InvalidState,
    InvalidInput,
    Capacity,
}

/// One exclusive boot-control owner. Requests apply on the next reset, without
/// asserting that the staged image is authentic; the bootloader checks that.
pub trait Control {
    fn state(&mut self) -> Result<State, DeviceError>;
    /// Erase and stream exactly `length` bytes into the inactive image slot,
    /// excluding boot metadata. Uses bounded scratch space and verifies writes.
    /// The source must end at `length`; any failure poisons this owner. This does
    /// not authenticate, activate or reboot; request a trial only after success.
    fn stage_update(
        &mut self,
        source: &mut dyn std::io::Read,
        length: u32,
    ) -> Result<(), DeviceError>;
    fn request_trial(&mut self) -> Result<(), DeviceError>;
    /// Call only after the application has passed its startup health policy.
    fn confirm(&mut self) -> Result<(), DeviceError>;
}

#[cfg(any(hal_enabled, test))]
mod control;
mod platform;
#[cfg(any(hal_enabled, test))]
mod staging;
pub use platform::open;

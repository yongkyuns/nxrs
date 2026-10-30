//! Trial and confirmation control. Image authentication and recovery belong to
//! the bootloader; application health must be established before confirmation.
#![no_std]
#![deny(unsafe_code)]

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
}

/// One exclusive boot-control owner. Requests apply on the next reset, without
/// asserting that the staged image is authentic; the bootloader checks that.
pub trait Control {
    fn state(&mut self) -> Result<State, DeviceError>;
    fn request_trial(&mut self) -> Result<(), DeviceError>;
    /// Call only after the application has passed its startup health policy.
    fn confirm(&mut self) -> Result<(), DeviceError>;
}

#[cfg(any(hal_enabled, test))]
mod control;
mod platform;
pub use platform::open;

//! Reset diagnostics, trial and confirmation control. The bootloader owns
//! image authentication and recovery. Confirm only after application health checks.
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
    InvalidInput,
    Capacity,
}

/// Hardware-reported cause of the most recent reset.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResetCause {
    /// The hardware may group power-on with other full chip resets.
    ChipReset,
    Brownout,
    Software,
    ExternalPin,
    LowPower,
    MainWatchdog,
    RtcWatchdog,
    Unknown,
}

/// One exclusive boot-control owner. Requests apply on the next reset, without
/// asserting that the staged image is authentic; the bootloader checks that.
pub trait Control {
    /// Reset only after a complete staged image has been admitted for trial.
    /// This never changes hardware protection or provisioning settings.
    fn reboot(&mut self) -> Result<core::convert::Infallible, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    /// Reset into the confirmed primary image only after reading Stable.
    fn reboot_confirmed(&mut self) -> Result<core::convert::Infallible, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn state(&mut self) -> Result<State, DeviceError>;
    /// Inactive-slot data capacity, excluding boot metadata; no erase or write.
    fn update_capacity(&mut self) -> Result<u32, DeviceError> {
        Err(DeviceError::Unsupported)
    }
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

cfg_if::cfg_if! {
    if #[cfg(any(platform = "nuttx", test))] {
        mod control;
        mod staging;
    }
}
mod platform;
pub use platform::{open, reset_cause};

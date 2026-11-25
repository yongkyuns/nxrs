//! Own a hardware reset watchdog. The profile selects an inherited deadline
//! or a timer that application code can arm and feed explicitly.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Io,
    InvalidState,
    InvalidTimeout,
}

/// One exclusive reset watchdog. Dropping the handle leaves the timer running.
/// For inherited deadlines, disarm only after health and firmware confirmation.
pub trait ArmedWatchdog {
    fn disarm(&mut self) -> Result<(), DeviceError>;
}

/// An application-armed reset timer. Feed only after checking application health.
pub trait RuntimeWatchdog: ArmedWatchdog {
    fn feed(&mut self) -> Result<(), DeviceError>;
}

mod platform;
pub use platform::{adopt, arm};

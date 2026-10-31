//! Adopt an inherited reset deadline without extending it. The bootloader owns
//! arming; application health policy owns explicit cancellation. Drop keeps the
//! watchdog running, including failed startup and unwinding.
#![no_std]
#![deny(unsafe_code)]
extern crate std;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Io,
    InvalidState,
}

/// One exclusive inherited reset watchdog. Cancel only after health and any
/// firmware confirmation have completed; uncertain cancellation stays sticky.
pub trait ArmedWatchdog {
    fn disarm(&mut self) -> Result<(), DeviceError>;
}

mod platform;
pub use platform::adopt;

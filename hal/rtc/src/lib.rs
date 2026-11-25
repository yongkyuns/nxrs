//! Direct access to a hardware real-time clock. Use `std::time::SystemTime::now`
//! for ordinary wall time and `std::time::Instant` for elapsed time.
#![deny(unsafe_code)]

use std::time::SystemTime;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    InvalidTime,
    Io,
}

pub trait Clock {
    /// Read the hardware clock at whole-second resolution. `None` means the
    /// device reports the clock is unset or unavailable. Some devices can
    /// only report that the counter is running. The caller owns time trust.
    fn read(&self) -> Result<Option<SystemTime>, Error>;

    /// Set UTC time between the Unix epoch and the end of year 2099. Fractions
    /// of a second are discarded. NuttX also synchronizes its system clock.
    fn set(&mut self, time: SystemTime) -> Result<(), Error>;
}

mod platform;
pub use platform::open;

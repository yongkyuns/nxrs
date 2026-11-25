//! On-demand battery state of charge and power-source telemetry.
#![deny(unsafe_code)]

pub use peripheral::battery::{Battery, ChargeState, Status};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    Io,
}

pub trait Device {
    /// Take a snapshot. A missing battery or estimate is represented in
    /// `Status`. An I/O failure fails the read.
    fn status(&self) -> Result<Status, Error>;
}

mod platform;

/// Construction is reserved for platform catalogues and qualification.
pub use platform::construction;

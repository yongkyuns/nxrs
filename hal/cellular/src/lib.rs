//! An owned cellular IPv4 link. Applications use ordinary Rust sockets once it is online.
#![deny(unsafe_code)]

use std::{fmt, net::Ipv4Addr};

mod platform;
pub use platform::connect;

#[cfg(test)]
mod test_support;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    InvalidApn,
    Busy,
    Rejected,
    Timeout,
    LinkLost,
    Io,
    RecoveryRequired,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Unsupported => "cellular is unavailable",
            Self::InvalidApn => "invalid cellular APN",
            Self::Busy => "a cellular session is already active",
            Self::Rejected => "the modem rejected its data connection",
            Self::Timeout => "cellular connection timed out",
            Self::LinkLost => "the cellular link was lost",
            Self::Io => "cellular I/O failed",
            Self::RecoveryRequired => "the modem did not return to command mode",
        })
    }
}

impl std::error::Error for Error {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Connecting,
    Online { address: Ipv4Addr },
    Stopped,
    Failed(Error),
}

/// One exclusive link. Dropping or stopping it joins its worker and removes the IP interface.
pub trait Session {
    fn status(&self) -> Status;

    /// Join teardown and report the terminal failure, if any.
    fn stop(&mut self) -> Result<(), Error>;
}

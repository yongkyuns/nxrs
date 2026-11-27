//! An owned cellular IPv4 link. Applications use ordinary Rust sockets once it is online.
#![deny(unsafe_code)]

use std::{fmt, net::Ipv4Addr, time::Duration};

mod platform;
#[cfg(any(platform = "nuttx", test))]
mod teardown;
pub use platform::{connect, connect_managed, LINK_AVAILABLE};

/// Maximum modem command-mode cleanup budget after cancellation is observed.
/// This software deadline includes both escape guards, writes and responses;
/// callers must also budget scheduler and resource-retirement latency.
pub const SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(10_200);

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
    Online {
        address: Ipv4Addr,
    },
    /// The managed link is down and the provider will attempt recovery.
    Retrying(Error),
    Stopped,
    /// The session has reached a terminal failure.
    Failed(Error),
}

/// One exclusive link. Stopping it joins its worker and removes the IP interface.
/// `connect_managed` retries recoverable failures and notifies its caller from
/// the worker when the retained status changes.
pub trait Session {
    fn status(&self) -> Status;

    /// Wake the worker and begin cancellation without joining it.
    fn request_stop(&mut self);

    /// Whether the worker has retired and can be joined without waiting for I/O.
    fn is_finished(&self) -> bool;

    /// Join teardown and report the terminal failure, if any.
    fn join(&mut self) -> Result<(), Error>;

    /// Cancel then join. Supervisors with a deadline should use the split API.
    fn stop(&mut self) -> Result<(), Error> {
        self.request_stop();
        self.join()
    }
}

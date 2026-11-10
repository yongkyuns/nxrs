//! WIFI contracts and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(platform = "nuttx", platform = "linux", platform = "macos", test))]
extern crate std;
mod api;
pub use api::{
    AccessPoint, Capabilities, Command, Completion, CredentialStorage, Credentials, DeliveryError,
    Error, Event, EventKind, LinkState, OperationId, Passphrase, ProviderStatus, QuiescenceControl,
    Rejected, Request, RequestId, Security, SessionId, Ssid, Wifi,
};
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
mod platform;
#[cfg(any(platform = "linux", platform = "macos", all(test, unix)))]
mod shared;
#[cfg(any(
    platform = "nuttx",
    platform = "linux",
    platform = "macos",
    all(test, unix)
))]
mod worker;

mod acquisition;
pub use acquisition::{open, open_for_quiescence, open_with_storage};

#[cfg(test)]
mod test_support;
#[cfg(all(test, unix, not(target_os = "nuttx")))]
use test_support::nuttx_tests;

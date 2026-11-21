//! WIFI contracts and OS-selected acquisition.
#![cfg_attr(target_os = "none", no_std)]
#![deny(unsafe_code)]

mod api;
pub use api::{
    AccessPoint, Capabilities, Command, Completion, CredentialStorage, Credentials, DeliveryError,
    Error, Event, EventKind, LinkState, OperationId, Passphrase, ProviderStatus, QuiescenceControl,
    Rejected, Request, RequestId, Security, SessionId, Ssid, Wifi,
};
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
mod platform;

mod acquisition;
pub use acquisition::{open, open_for_quiescence, open_with_storage};

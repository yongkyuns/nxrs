//! BLE contracts and OS-selected acquisition.
#![cfg_attr(target_os = "none", no_std)]
#![deny(unsafe_code)]

mod api;
pub use api::{
    Ble, Capabilities, Command, Completion, DeliveryError, Error, Event, EventKind, OperationId,
    PeerId, ProviderStatus, Rejected, Request, RequestId, SessionId, Uuid,
};
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
pub mod gatt;
mod platform;

/// Open the selected BLE provider, or return `Unsupported` when unbound.
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
{
    platform::open(sink).map(Controller::new)
}

#[cfg(feature = "peripheral")]
pub mod peripheral;

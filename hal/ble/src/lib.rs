//! BLE contracts and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_provider = "hardware", test))]
extern crate std;
mod api;
pub use api::{
    Ble, Capabilities, Command, Completion, DeliveryError, Error, Event, EventKind, OperationId,
    PeerId, ProviderStatus, Rejected, Request, RequestId, SessionId, Uuid,
};
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
pub mod gatt;
#[cfg(any(hal_provider = "hardware", all(test, unix)))]
mod gatt_backend;
#[cfg(any(hal_provider = "hardware", all(test, unix)))]
mod gatt_driver;
#[cfg(hal_provider = "hardware")]
mod platform;
#[cfg(any(
    all(hal_provider = "hardware", not(target_os = "nuttx")),
    all(test, unix)
))]
mod shared;
#[cfg(any(hal_provider = "hardware", all(test, unix)))]
mod worker;

#[cfg(hal_provider = "hardware")]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
{
    platform::open(sink).map(Controller::new)
}

#[cfg(test)]
mod test_support;

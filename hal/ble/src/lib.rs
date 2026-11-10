//! BLE contracts and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(platform = "nuttx", platform = "linux", platform = "macos", test))]
extern crate std;
mod api;
pub use api::{
    Ble, Capabilities, Command, Completion, DeliveryError, Error, Event, EventKind, OperationId,
    PeerId, ProviderStatus, Rejected, Request, RequestId, SessionId, Uuid,
};
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
pub mod gatt;
cfg_if::cfg_if! {
    if #[cfg(any(
        platform = "nuttx",
        platform = "linux",
        platform = "macos",
        all(test, unix)
    ))] {
        mod gatt_backend;
        mod gatt_driver;
        mod worker;
    }
}
#[cfg(any(platform = "linux", platform = "macos", all(test, unix)))]
mod shared;

cfg_if::cfg_if! {
    if #[cfg(any(platform = "nuttx", platform = "linux", platform = "macos"))] {
        mod platform;

        pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
        where
            S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
        {
            platform::open(sink).map(Controller::new)
        }
    }
}

#[cfg(test)]
mod test_support;

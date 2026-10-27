//! WIFI contracts and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(hal_enabled, test))]
extern crate std;
mod api;
pub use api::*;
mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};
#[cfg(any(hal_enabled, all(test, unix)))]
mod platform;
#[cfg(any(all(hal_enabled, not(target_os = "nuttx")), all(test, unix)))]
mod shared;
#[cfg(any(hal_enabled, all(test, unix)))]
mod worker;

#[cfg(hal_enabled)]
pub fn open<S>(sink: S) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    open_with_storage(sink, CredentialStorage::Volatile)
}

/// Acquire Wi-Fi with an explicit credential persistence policy.
#[cfg(hal_enabled)]
pub fn open_with_storage<S>(
    sink: S,
    storage: CredentialStorage,
) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    platform::open(sink, storage).map(Controller::new)
}

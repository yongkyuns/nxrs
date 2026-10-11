//! BLE controller and contracts; platform configuration selects acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(test, nxrs_provider = "nuttx"))]
extern crate std;

mod api;
pub use api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

#[cfg(any(nxrs_provider = "nuttx", all(test, unix)))]
mod providers;

#[cfg(any(nxrs_provider = "nuttx", all(test, unix)))]
mod worker;

#[cfg(nxrs_provider = "nuttx")]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    providers::nuttx::open(sink).map(Controller::new)
}

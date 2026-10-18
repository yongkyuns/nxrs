//! BLE controller and contracts; platform configuration selects acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(test, nxrs_provider = "nuttx", nxrs_provider = "native"))]
extern crate std;

mod api;
pub use api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

pub mod gatt;

#[cfg(any(nxrs_provider = "nuttx", nxrs_provider = "native", all(test, unix)))]
mod providers;

#[cfg(any(nxrs_provider = "nuttx", nxrs_provider = "native", all(test, unix)))]
mod worker;

#[cfg(all(
    nxrs_provider = "native",
    not(any(target_os = "macos", target_os = "linux"))
))]
compile_error!("native BLE is not implemented for this target OS");

#[cfg(nxrs_provider = "nuttx")]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    providers::nuttx::open(sink).map(Controller::new)
}

#[cfg(all(
    nxrs_provider = "native",
    any(target_os = "macos", target_os = "linux")
))]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    providers::native::open(sink).map(Controller::new)
}

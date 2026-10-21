//! BLE controller and contracts; platform configuration selects acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(
    test,
    nxrs_hal_platform = "nuttx",
    nxrs_hal_platform = "linux",
    nxrs_hal_platform = "macos"
))]
extern crate std;

mod api;
pub use api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

pub mod gatt;

#[cfg(any(
    nxrs_hal_platform = "nuttx",
    nxrs_hal_platform = "linux",
    nxrs_hal_platform = "macos",
    all(test, unix)
))]
mod platform;

#[cfg(any(
    nxrs_hal_platform = "nuttx",
    nxrs_hal_platform = "linux",
    nxrs_hal_platform = "macos",
    all(test, unix)
))]
mod worker;

#[cfg(all(
    any(nxrs_hal_platform = "linux", nxrs_hal_platform = "macos"),
    not(any(target_os = "macos", target_os = "linux"))
))]
compile_error!("native BLE is not implemented for this target OS");

#[cfg(nxrs_hal_platform = "nuttx")]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    platform::nuttx::open(sink).map(Controller::new)
}

#[cfg(any(nxrs_hal_platform = "linux", nxrs_hal_platform = "macos"))]
pub fn open<S>(sink: S) -> Result<Controller<impl Ble + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    platform::open(sink).map(Controller::new)
}

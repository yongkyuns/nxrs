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

#[cfg(nxrs_provider = "nuttx")]
pub use providers::nuttx::{NativeScanner, NuttxBle, Record};

#[cfg(nxrs_provider = "nuttx")]
pub fn open<S, const N: usize>(sink: S) -> Result<Controller<impl Ble<N> + Send, N>, Error>
where
    S: EventSink<Event<N>> + Send + 'static,
{
    NuttxBle::open(sink).map(Controller::new)
}

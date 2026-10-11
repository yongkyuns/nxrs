//! BLE controller and contracts; the build selects native acquisition.
#![no_std]
#![forbid(unsafe_code)]

pub use ble_api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

#[cfg(feature = "nuttx")]
pub fn open<S, const N: usize>(sink: S) -> Result<Controller<impl Ble<N> + Send, N>, Error>
where
    S: EventSink<Event<N>> + Send + 'static,
{
    ble_nuttx::NuttxBle::open(sink).map(Controller::new)
}

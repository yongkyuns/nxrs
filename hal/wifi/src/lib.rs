//! Wi-Fi controller and contracts; the build selects native acquisition.
#![no_std]
#![forbid(unsafe_code)]

pub use wifi_api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

#[cfg(feature = "nuttx")]
pub fn open<S>(sink: S) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    wifi_nuttx::NuttxWifi::open(sink).map(Controller::new)
}

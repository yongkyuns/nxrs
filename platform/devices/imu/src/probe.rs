//! Diagnostics for the selected physical device, enabled by the `probe` feature.
use crate::selected as platform;
pub use imu::probe::Timing;
use imu::{AppliedConfig, Config, DeviceError, Event, Session};
use std::boxed::Box;

/// Open the selected physical provider with a nonblocking observer. Ignore
/// ioctl observations outside a wake/drain pair when measuring steady state.
pub fn open_configured(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(Timing) + Send + Sync + 'static,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    platform::open_probed(config, sink, observe, false)
}

/// Open a physical probe with the sensor-specific external data-ready reference
/// enabled. This explicit qualification operation changes sensor interrupt
/// routing. Providers without physical timing support return `Unsupported`.
pub fn open_sensor_reference_configured(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(Timing) + Send + Sync + 'static,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    platform::open_probed(config, sink, observe, true)
}

#[allow(dead_code, reason = "Only providers without timing use this fallback.")]
pub(crate) fn unsupported(
    _: Config,
    _: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    _: impl Fn(Timing) + Send + Sync + 'static,
    _: bool,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    Err(DeviceError::Unsupported)
}

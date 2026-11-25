//! Diagnostics for the selected physical device, enabled by the `probe` feature.
#[cfg(device_binding = "imu:nuttx")]
use crate::imu_factory::open_probed;
use ::imu::{AppliedConfig, Config, DeviceError, Event, Session};
pub use acquisition::probe::Timing;
use std::boxed::Box;
#[cfg(not(device_binding = "imu:nuttx"))]
use unsupported as open_probed;

/// Open the selected physical provider with a nonblocking observer. Ignore
/// ioctl observations outside a wake/drain pair when measuring steady state.
pub fn open_configured(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(Timing) + Send + Sync + 'static,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    open_probed(config, sink, observe, false)
}

/// Open a physical probe with the sensor-specific external data-ready reference
/// enabled. This explicit qualification operation changes sensor interrupt
/// routing. Providers without physical timing support return `Unsupported`.
pub fn open_sensor_reference_configured(
    config: Config,
    sink: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    observe: impl Fn(Timing) + Send + Sync + 'static,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    open_probed(config, sink, observe, true)
}

#[cfg(not(device_binding = "imu:nuttx"))]
fn unsupported(
    _: Config,
    _: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    _: impl Fn(Timing) + Send + Sync + 'static,
    _: bool,
) -> Result<(Box<dyn Session + Send>, AppliedConfig), DeviceError> {
    Err(DeviceError::Unsupported)
}

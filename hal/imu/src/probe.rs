//! Timing diagnostics, compiled with `probe` and activated by an observer.
//! Without the feature, opens return `Unsupported` before resource acquisition.
//! Observer callbacks run in task context and must not block.
use crate::{platform, AppliedConfig, Config, DeviceError, Event, Session};
use std::{boxed::Box, time::Instant};

#[derive(Clone, Copy, Debug)]
pub enum Timing {
    /// Userspace has finished waiting for readiness and acknowledging GPIO.
    Wake {
        at: Instant,
        periodic_deadline: Option<Instant>,
    },
    /// Synchronous I²C transaction interval, including adapter checks and kernel wait.
    Ioctl { start: Instant, end: Instant },
    /// FIFO drain returned, including decoding and sample delivery. A failure
    /// or early termination sets `complete` to false.
    Drain { at: Instant, complete: bool },
}

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

//! Optional timing observations from physical acquisition task context.
use crate::{platform, AppliedConfig, Config, DeviceError, Event, Imu};
use std::{boxed::Box, time::Instant};

#[derive(Clone, Copy, Debug)]
pub enum Timing {
    /// Userspace has finished waiting for readiness and acknowledging GPIO.
    Wake {
        at: Instant,
        periodic_deadline: Option<Instant>,
    },
    /// Synchronous I²C ioctl interval, including any kernel wait.
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
) -> Result<(Box<dyn Imu + Send>, AppliedConfig), DeviceError> {
    platform::open_probed(config, sink, observe)
}

#[allow(dead_code, reason = "Only providers without timing use this fallback.")]
pub(crate) fn unsupported(
    _: Config,
    _: impl FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
    _: impl Fn(Timing) + Send + Sync + 'static,
) -> Result<(Box<dyn Imu + Send>, AppliedConfig), DeviceError> {
    Err(DeviceError::Unsupported)
}

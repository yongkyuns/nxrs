//! Compatibility access to the primary device through the ordinary contract.
use crate::{AppliedConfig, Config, Device, DeviceError, Event, Session};
pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Session + Send + 'static, DeviceError> {
    crate::primary().open_default(sink)
}
pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
    crate::primary().open(config, sink)
}
pub fn resolve(config: Config) -> Result<AppliedConfig, DeviceError> {
    crate::primary().resolve(config)
}

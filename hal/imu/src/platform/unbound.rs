use crate::{AppliedConfig, Capabilities, Config, DeviceError, Event, Imu};
struct Unconfigured;
impl Imu for Unconfigured {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        Ok(())
    }
    fn join(&mut self) -> Result<(), DeviceError> {
        Ok(())
    }
    fn is_finished(&self) -> bool {
        true
    }
    fn dropped(&self) -> u32 {
        0
    }
}
pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    _: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    Err::<Unconfigured, _>(DeviceError::Unsupported)
}

pub fn capabilities() -> Option<Capabilities> {
    None
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    _: Config,
    _: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    Err::<(Unconfigured, AppliedConfig), _>(DeviceError::Unsupported)
}

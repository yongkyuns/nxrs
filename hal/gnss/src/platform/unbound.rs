use crate::{DeviceError, Event, Gnss};
struct Unconfigured;
impl Gnss for Unconfigured {
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
) -> Result<impl Gnss + Send + 'static, DeviceError> {
    Err::<Unconfigured, _>(DeviceError::Unsupported)
}

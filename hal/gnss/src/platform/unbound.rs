pub use crate::fixtures::devices;
use crate::{DeviceError, Event, Session};
struct Unconfigured;
impl Session for Unconfigured {
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
) -> Result<impl Session + Send + 'static, DeviceError> {
    Err::<Unconfigured, _>(DeviceError::Unsupported)
}

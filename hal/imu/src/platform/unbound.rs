use crate::{DeviceError, Event, Imu};
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

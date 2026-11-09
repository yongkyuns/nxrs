use crate::{AppliedConfig, Config, DeviceError, Event, Session};

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
struct SelectedDevice(&'static str);
static PRIMARY: SelectedDevice = SelectedDevice("primary");
static SECONDARY: SelectedDevice = SelectedDevice("secondary");
impl crate::api::Sealed for SelectedDevice {}
impl crate::Device for SelectedDevice {
    fn id(&self) -> &'static str {
        self.0
    }
    fn default_config(&self) -> Result<Config, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn resolve(&self, _: Config) -> Result<AppliedConfig, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        _: Config,
        _: S,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        Err::<(Unconfigured, AppliedConfig), _>(DeviceError::Unsupported)
    }
}
pub mod devices {
    pub fn primary_imu() -> &'static impl crate::Device {
        &super::PRIMARY
    }
    pub fn secondary() -> &'static impl crate::Device {
        &super::SECONDARY
    }
}

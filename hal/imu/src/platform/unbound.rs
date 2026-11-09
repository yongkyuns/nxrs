pub(crate) use crate::probe::unsupported as open_probed;
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
/// A profile must bind an IMU provider before settings can be resolved.
pub fn resolve(_: Config) -> Result<AppliedConfig, DeviceError> {
    Err(DeviceError::Unsupported)
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    _: Config,
    _: S,
) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
    Err::<(Unconfigured, AppliedConfig), _>(DeviceError::Unsupported)
}

struct SelectedDevice(&'static str, crate::instance::Ownership);
static PRIMARY: SelectedDevice = SelectedDevice("primary", crate::instance::Ownership::new());
static SECONDARY: SelectedDevice = SelectedDevice("secondary", crate::instance::Ownership::new());
impl crate::instance::Sealed for SelectedDevice {}
impl crate::Device for SelectedDevice {
    fn id(&self) -> &'static str {
        self.0
    }
    fn default_config(&self) -> Result<Config, DeviceError> {
        Err(DeviceError::Unsupported)
    }
    fn resolve(&self, config: Config) -> Result<AppliedConfig, DeviceError> {
        resolve(config)
    }
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        config: Config,
        sink: S,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        resolve(config)?;
        let mut retained = (sink, self.1.take()?);
        open_configured(config, move |event| {
            let retained = &mut retained; // Capture the whole tuple; sink drops before lease.
            (retained.0)(event)
        })
    }
}
pub mod devices {
    pub fn primary() -> &'static impl crate::Device {
        &super::PRIMARY
    }
    pub fn secondary() -> &'static impl crate::Device {
        &super::SECONDARY
    }
}
pub use devices::primary;

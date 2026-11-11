//! Named access to explicit mock/simulation sources and the unbound result.
use crate::{api::Sealed, Device, DeviceError, Event, Session};
struct Configured;
static PRIMARY: Configured = Configured;
impl Sealed for Configured {}
impl Device for Configured {
    fn id(&self) -> &'static str {
        "primary_gnss"
    }
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        sink: S,
    ) -> Result<impl Session + Send + 'static, DeviceError> {
        crate::platform::selected::open(sink)
    }
}
pub mod devices {
    pub fn primary_gnss() -> &'static impl crate::Device {
        &super::PRIMARY
    }
}

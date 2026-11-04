use crate::{DeviceError, Led, LedSet};
struct UnconfiguredLed;

impl Led for UnconfiguredLed {
    fn supported(&self) -> LedSet {
        LedSet::default()
    }
    fn set(&mut self, _index: u8, _on: bool) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

pub fn open() -> Result<impl Led, DeviceError> {
    Err::<UnconfiguredLed, _>(DeviceError::Unsupported)
}

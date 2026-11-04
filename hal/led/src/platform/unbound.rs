use crate::{DeviceError, Led, LedSet};
pub struct UnconfiguredLed;

impl Led for UnconfiguredLed {
    fn supported(&self) -> LedSet {
        LedSet::default()
    }
    fn set(&mut self, _index: u8, _on: bool) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

pub fn open() -> Result<UnconfiguredLed, DeviceError> {
    Err(DeviceError::Unsupported)
}

//! Admit one implementation, with source-injected NuttX witnesses in unit tests.
#[cfg(any(test, hal_mock))]
mod mock;
#[cfg(any(hal_enabled, all(test, unix)))]
#[allow(unsafe_code)]
mod nuttx;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::MockLeds;
    pub fn open() -> Result<impl crate::Led + Send + 'static, crate::DeviceError> {
        Ok(MockLeds::default())
    }
}
#[cfg(hal_enabled)]
mod selected {
    pub use super::nuttx::UserLeds;
    pub fn open() -> Result<impl crate::Led + Send + 'static, crate::DeviceError> {
        UserLeds::open()
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
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
}
pub use selected::*;

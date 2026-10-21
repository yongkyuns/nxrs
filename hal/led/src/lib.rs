//! Typed LED capability and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(nxrs_hal_platform = "nuttx", all(test, unix)))]
extern crate std;

mod api;
pub use api::{DeviceError, Led, LedSet};

#[cfg(any(test, nxrs_hal_platform = "mock"))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(nxrs_hal_platform = "mock")]
pub use mock::MockLeds;

#[cfg(any(nxrs_hal_platform = "nuttx", all(test, unix)))]
#[allow(unsafe_code)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(nxrs_hal_platform = "nuttx")]
pub use nuttx::UserLeds;

#[cfg(nxrs_hal_platform = "mock")]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    Ok(MockLeds::default())
}

#[cfg(nxrs_hal_platform = "nuttx")]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    nuttx::UserLeds::open()
}

#[cfg(nxrs_hal_platform = "none")]
pub struct UnconfiguredLed;

#[cfg(nxrs_hal_platform = "none")]
impl Led for UnconfiguredLed {
    fn supported(&self) -> LedSet {
        LedSet::default()
    }
    fn set(&mut self, _index: u8, _on: bool) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(nxrs_hal_platform = "none")]
pub fn open() -> Result<UnconfiguredLed, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(nxrs_hal_platform = "none")]
    #[test]
    fn unconfigured_never_silently_selects_a_mock() {
        assert!(matches!(open(), Err(DeviceError::Unsupported)));
    }

    #[cfg(nxrs_hal_platform = "mock")]
    #[test]
    fn portable_consumer_uses_only_the_capability() {
        let mut leds = open().unwrap();
        assert!(leds.supported().contains(0));
        leds.set(0, true).unwrap();
        leds.set(0, false).unwrap();
        assert_eq!(leds.set(1, true), Err(DeviceError::Unsupported));
    }
}

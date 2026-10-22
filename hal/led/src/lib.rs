//! Typed LED capability and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
extern crate std;

mod api;
pub use api::{DeviceError, Led, LedSet};

#[cfg(any(test, hal_mock))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(hal_mock)]
pub use mock::MockLeds;

#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
#[allow(unsafe_code)]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub use nuttx::UserLeds;

#[cfg(hal_mock)]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    Ok(MockLeds::default())
}

#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    nuttx::UserLeds::open()
}

#[cfg(not(any(hal_enabled, hal_mock)))]
pub struct UnconfiguredLed;

#[cfg(not(any(hal_enabled, hal_mock)))]
impl Led for UnconfiguredLed {
    fn supported(&self) -> LedSet {
        LedSet::default()
    }
    fn set(&mut self, _index: u8, _on: bool) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(not(any(hal_enabled, hal_mock)))]
pub fn open() -> Result<UnconfiguredLed, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(any(hal_enabled, hal_mock)))]
    #[test]
    fn unconfigured_never_silently_selects_a_mock() {
        assert!(matches!(open(), Err(DeviceError::Unsupported)));
    }

    #[cfg(hal_mock)]
    #[test]
    fn portable_consumer_uses_only_the_capability() {
        let mut leds = open().unwrap();
        assert!(leds.supported().contains(0));
        leds.set(0, true).unwrap();
        leds.set(0, false).unwrap();
        assert_eq!(leds.set(1, true), Err(DeviceError::Unsupported));
    }
}

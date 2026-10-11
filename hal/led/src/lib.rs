//! LED capability facade; the build, not a service, selects its provider.
#![no_std]
#![forbid(unsafe_code)]

pub use led_api::{DeviceError, Led, LedSet};

#[cfg(all(feature = "mock", feature = "nuttx"))]
compile_error!("LED providers are mutually exclusive: select mock OR nuttx");

#[cfg(all(feature = "mock", not(feature = "nuttx")))]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    Ok(led_mock::MockLeds::default())
}

#[cfg(all(feature = "nuttx", not(feature = "mock")))]
pub fn open() -> Result<impl Led + Send + 'static, DeviceError> {
    led_nuttx::UserLeds::open()
}

#[cfg(not(any(feature = "mock", feature = "nuttx")))]
pub struct UnconfiguredLed;

#[cfg(not(any(feature = "mock", feature = "nuttx")))]
impl Led for UnconfiguredLed {
    fn supported(&self) -> LedSet { LedSet::default() }
    fn set(&mut self, _index: u8, _on: bool) -> Result<(), DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(not(any(feature = "mock", feature = "nuttx")))]
pub fn open() -> Result<UnconfiguredLed, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(any(feature = "mock", feature = "nuttx")))]
    #[test]
    fn unconfigured_never_silently_selects_a_mock() {
        assert!(matches!(open(), Err(DeviceError::Unsupported)));
    }

    #[cfg(all(feature = "mock", not(feature = "nuttx")))]
    #[test]
    fn portable_consumer_uses_only_the_capability() {
        let mut leds = open().unwrap();
        assert!(leds.supported().contains(0));
        leds.set(0, true).unwrap();
        leds.set(0, false).unwrap();
        assert_eq!(leds.set(1, true), Err(DeviceError::Unsupported));
    }
}

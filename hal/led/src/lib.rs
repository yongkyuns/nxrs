//! LED contract and capability-local acquisition.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(hal_enabled, test))]
extern crate std;
mod api;
pub use api::{DeviceError, Led, LedSet};
mod platform;
pub use platform::*;

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

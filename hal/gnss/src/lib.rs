//! GNSS fix contract and platform-selected provider.
#![no_std]
#![deny(unsafe_code)]

mod api;
pub use api::{DeviceError, Gnss, GnssFix};

#[cfg(any(test, nxrs_hal_platform = "mock"))]
#[path = "platform/mock.rs"]
mod mock;
#[cfg(nxrs_hal_platform = "mock")]
pub use mock::SyntheticGnss;

#[cfg(nxrs_hal_platform = "mock")]
pub fn open() -> Result<impl Gnss + Send + 'static, DeviceError> {
    Ok(SyntheticGnss::default())
}

#[cfg(nxrs_hal_platform = "none")]
pub struct UnconfiguredGnss;

#[cfg(nxrs_hal_platform = "none")]
impl Gnss for UnconfiguredGnss {
    fn fix(&mut self, _now_ms: u64) -> Result<GnssFix, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(nxrs_hal_platform = "none")]
pub fn open() -> Result<UnconfiguredGnss, DeviceError> {
    Err(DeviceError::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn synthetic_fixes_have_increasing_sequences_and_caller_timestamps() {
        let mut gnss = mock::SyntheticGnss::default();
        let first = gnss.fix(10).unwrap();
        let second = gnss.fix(20).unwrap();
        assert_eq!((first.sequence, second.sequence), (1, 2));
        assert_eq!((first.timestamp_ms, second.timestamp_ms), (10, 20));
        assert!(second.east_m > first.east_m);
    }
}

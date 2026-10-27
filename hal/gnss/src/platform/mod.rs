//! Capability admission; unsupported real providers are rejected by the profile.
#[cfg(any(test, hal_mock))]
pub(super) mod mock;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::SyntheticGnss;
    pub fn open() -> Result<impl crate::Gnss + Send + 'static, crate::DeviceError> {
        Ok(SyntheticGnss::default())
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{DeviceError, Gnss, GnssFix};
    pub struct UnconfiguredGnss;

    impl Gnss for UnconfiguredGnss {
        fn fix(&mut self, _now_ms: u64) -> Result<GnssFix, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }

    pub fn open() -> Result<UnconfiguredGnss, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}
pub use selected::*;

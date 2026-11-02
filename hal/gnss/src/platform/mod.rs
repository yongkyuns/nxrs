//! Capability admission and resource acquisition.
#[cfg(any(test, hal_mock))]
pub(super) mod mock;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
#[path = "nuttx/mod.rs"]
mod selected;
#[cfg(hal_mock)]
mod selected {
    pub fn open() -> Result<impl crate::Gnss + Send + 'static, crate::DeviceError> {
        Ok(super::mock::SyntheticGnss::default())
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{DeviceError, Gnss, GnssFix};
    struct UnconfiguredGnss;

    impl Gnss for UnconfiguredGnss {
        fn fix(&mut self, _now_ms: u64) -> Result<GnssFix, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }

    pub fn open() -> Result<impl Gnss + Send + 'static, DeviceError> {
        Err::<UnconfiguredGnss, _>(DeviceError::Unsupported)
    }
}
pub use selected::open;

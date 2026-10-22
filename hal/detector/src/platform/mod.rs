//! Acquisition wiring: the profile admits a capability; the target selects its OS.
use crate::{Config, Detector, DeviceError};

#[cfg(all(hal_enabled, any(feature = "person-model", feature = "pico-model")))]
#[cfg_attr(target_os = "linux", path = "linux/mod.rs")]
#[cfg_attr(target_os = "macos", path = "macos/mod.rs")]
#[cfg_attr(target_os = "windows", path = "windows/mod.rs")]
#[cfg_attr(target_os = "nuttx", path = "nuttx/mod.rs")]
mod selected;

pub fn open(config: Config) -> Result<impl Detector, DeviceError> {
    acquire(config)
}
#[cfg(all(hal_enabled, any(feature = "person-model", feature = "pico-model")))]
use selected::open as acquire;
#[cfg(not(all(hal_enabled, any(feature = "person-model", feature = "pico-model"))))]
fn acquire(_: Config) -> Result<impl Detector, DeviceError> {
    struct Unavailable;
    impl Detector for Unavailable {
        fn capabilities(&self) -> crate::Capabilities {
            crate::Capabilities::presence(0, 0)
        }
        fn detect(&mut self, _: crate::Image<'_>) -> Result<crate::DetectionResult, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

#[cfg(all(feature = "qualification", hal_enabled))]
pub(crate) use selected::kernel;
#[cfg(all(feature = "qualification", not(hal_enabled)))]
pub(crate) fn kernel() -> Result<crate::model::pico::Engine, DeviceError> {
    Err(DeviceError::Unsupported)
}

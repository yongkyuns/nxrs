//! GNSS capability facade. Provider selection is local to this HAL.
#![no_std]
#![forbid(unsafe_code)]

pub use gnss_api::{DeviceError, Gnss, GnssFix};

#[cfg(feature = "mock")]
pub fn open() -> Result<impl Gnss + Send + 'static, DeviceError> {
    Ok(gnss_mock::SyntheticGnss::default())
}

#[cfg(not(feature = "mock"))]
pub struct UnconfiguredGnss;

#[cfg(not(feature = "mock"))]
impl Gnss for UnconfiguredGnss {
    fn fix(&mut self, _now_ms: u64) -> Result<GnssFix, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}

#[cfg(not(feature = "mock"))]
pub fn open() -> Result<UnconfiguredGnss, DeviceError> {
    Err(DeviceError::Unsupported)
}

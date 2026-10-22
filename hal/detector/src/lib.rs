//! Detector contract, portable model behavior and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(feature = "person-model", detector_pico))]
extern crate std;
mod api;
pub use api::{
    Bounds, Capabilities, Config, Detection, DetectionResult, Detections, Detector, DeviceError,
    Image, Score,
};
#[cfg(detector_pico)]
mod compute;
#[cfg(any(feature = "person-model", detector_pico))]
mod model;
#[cfg(all(hal_enabled, any(feature = "person-model", detector_pico)))]
mod platform;
#[cfg(all(hal_enabled, any(feature = "person-model", detector_pico)))]
pub fn open(config: Config) -> Result<impl Detector, DeviceError> {
    platform::open(config)
}
#[cfg(feature = "person-model")]
pub use model::person::{MobileNet, MODEL_SHA256 as VWW_MODEL_SHA256};
#[cfg(all(feature = "qualification", detector_pico))]
pub mod qualification;
#[cfg(not(all(hal_enabled, any(feature = "person-model", detector_pico))))]
pub fn open(_: Config) -> core::result::Result<impl Detector, DeviceError> {
    struct Unavailable;
    impl Detector for Unavailable {
        fn capabilities(&self) -> Capabilities {
            Capabilities::presence(0, 0)
        }
        fn detect(&mut self, _: Image<'_>) -> core::result::Result<DetectionResult, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

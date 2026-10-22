//! Detector capability: a common Rust API over selected platform implementations.
#![no_std]
#![deny(unsafe_code)]
#[cfg(any(feature = "person-model", nxrs_detector_pico))]
extern crate std;
mod api;
pub use api::{
    Bounds, Capabilities, Config, Detection, DetectionResult, Detections, Detector, DeviceError,
    Image, Score,
};
#[cfg(all(
    nxrs_hal_platform = "nuttx",
    any(feature = "person-model", nxrs_detector_pico)
))]
#[path = "platform/nuttx/mod.rs"]
mod nuttx;
#[cfg(any(feature = "person-model", nxrs_detector_pico))]
#[path = "platform/std/mod.rs"]
mod standard;
// Existing raw-model qualification clients retain this export. Product code
// acquires `open`, which enforces the selected platform and requested capability.
#[cfg(feature = "person-model")]
pub use standard::person::{MODEL_SHA256 as VWW_MODEL_SHA256, MobileNet};
#[cfg(all(
    nxrs_hal_platform = "std",
    any(feature = "person-model", nxrs_detector_pico)
))]
pub fn open(config: Config) -> core::result::Result<impl Detector, DeviceError> {
    #[cfg(nxrs_detector_pico)]
    {
        let engine = standard::ffi::Engine::new().map_err(|_| DeviceError::Model)?;
        standard::with_kernel(config, engine)
    }
    #[cfg(not(nxrs_detector_pico))]
    {
        standard::open(config)
    }
}
#[cfg(all(
    nxrs_hal_platform = "nuttx",
    any(feature = "person-model", nxrs_detector_pico)
))]
pub fn open(config: Config) -> core::result::Result<impl Detector, DeviceError> {
    nuttx::open(config)
}
#[cfg(not(any(
    all(
        nxrs_hal_platform = "std",
        any(feature = "person-model", nxrs_detector_pico)
    ),
    all(
        nxrs_hal_platform = "nuttx",
        any(feature = "person-model", nxrs_detector_pico)
    )
)))]
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

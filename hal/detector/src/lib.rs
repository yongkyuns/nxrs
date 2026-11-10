//! Detector contract, portable model behavior and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

mod api;
pub use api::{
    Bounds, Capabilities, Config, Detection, DetectionResult, Detections, Detector, DeviceError,
    Image, Score,
};
// Model compilation does not require a bound acquisition factory.
cfg_if::cfg_if! {
    if #[cfg(any(feature = "person-model", feature = "pico-model"))] {
        extern crate std;
        #[cfg_attr(
            not(any(
                hal_provider = "linux",
                hal_provider = "macos",
                hal_provider = "windows",
                hal_provider = "nuttx"
            )),
            allow(dead_code, unused_imports)
        )]
        mod model;
        #[cfg(feature = "person-model")]
        pub use model::person::{MobileNet, MODEL_SHA256 as VWW_MODEL_SHA256};
    }
}
mod platform;
pub use platform::open;
#[cfg(all(feature = "probe", feature = "pico-model"))]
pub mod qualification;

/// Selected detector's execution requirements; absence means no configured detector.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecutionBudget {
    pub stack_bytes: usize,
    pub sample_interval_ms: u64,
}
mod budget;
pub use budget::execution_budget;

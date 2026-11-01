//! Detector contract, portable model behavior and OS-selected acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(feature = "person-model", feature = "pico-model"))]
extern crate std;
mod api;
pub use api::{
    Bounds, Capabilities, Config, Detection, DetectionResult, Detections, Detector, DeviceError,
    Image, Score,
};
// Model compilation does not require a bound acquisition factory.
#[cfg(any(feature = "person-model", feature = "pico-model"))]
#[cfg_attr(not(hal_enabled), allow(dead_code, unused_imports))]
mod model;
mod platform;
#[cfg(feature = "person-model")]
pub use model::person::{MobileNet, MODEL_SHA256 as VWW_MODEL_SHA256};
pub use platform::open;
#[cfg(all(feature = "probe", feature = "pico-model"))]
pub mod qualification;

/// Selected detector's execution requirements; absence means no configured detector.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecutionBudget {
    pub stack_bytes: usize,
    pub sample_interval_ms: u64,
}
pub use platform::execution_budget;

//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod api;
mod platform;
pub mod probe;
/// Compatibility name for the acquisition session; use `Session` in new code.
pub use api::Session as Imu;
pub use api::{AppliedConfig, Config, DeviceError, Event, ImuSample, Session};
pub use messaging::{DeliveryError, Rejected};
pub use platform::{open, open_configured, resolve};

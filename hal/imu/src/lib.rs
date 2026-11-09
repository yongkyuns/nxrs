//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod access;
mod api;
mod instance;
mod platform;
pub mod probe;
pub use access::{open, open_configured, resolve};
/// Compatibility name for the acquisition session; use `Session` in new code.
pub use api::Session as Imu;
pub use api::{AppliedConfig, Config, Device, DeviceError, Event, ImuSample, Session};
pub use messaging::{DeliveryError, Rejected};
pub use platform::{devices, primary};

//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod api;
mod platform;
pub mod probe;
pub use api::{AppliedConfig, Config, DeviceError, Event, Imu, ImuSample};
pub use messaging::{DeliveryError, Rejected};
pub use platform::{open, open_configured, resolve};

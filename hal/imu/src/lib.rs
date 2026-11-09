//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod api;
mod platform;
pub mod probe;
mod session;
pub use api::{AppliedConfig, Config, Device, DeviceError, Event, ImuSample, Session};
pub use messaging::{DeliveryError, Rejected};
pub use platform::{devices, primary};

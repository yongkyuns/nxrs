//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod api;
#[allow(dead_code, reason = "Unbound profiles do not acquire devices.")]
mod device_state;
mod platform;
mod session;
pub use api::{AppliedConfig, Config, Device, DeviceError, Event, ImuSample, Session};
pub use messaging::{DeliveryError, Rejected};
/// Construction for platform catalogues; applications use their named device accessors.
pub use platform::construction;

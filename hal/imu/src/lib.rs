//! IMU contract and capability-local acquisition.
#![deny(unsafe_code)]
mod api;
mod platform;
pub mod probe;
mod session;
pub use api::{AppliedConfig, Config, Device, DeviceError, Event, ImuSample, Session};
pub use messaging::{DeliveryError, Rejected};
/// Construction for platform catalogues; applications use their named device accessors.
pub use platform::construction;

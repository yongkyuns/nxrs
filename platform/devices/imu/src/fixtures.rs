//! Explicit fixture/unbound sources reuse the HAL's existing implementations.
pub(crate) use crate::probe::unsupported as open_probed;
pub use imu::construction::devices;

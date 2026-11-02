//! Active std services for the navigation demo.
//!
//! A service owns useful functionality and the platform capabilities required
//! to implement it. IMU and GNSS acquire their HAL resources internally at
//! start and move them into owner threads. Each active service owns one bounded
//! event inbox, one logical wait point, and handles each event to completion.
//! Fusion uses the same model for IMU/GNSS fan-in. Health remains synchronous
//! to show that a service does not require a thread.
#![forbid(unsafe_code)]

mod fusion;
mod gnss;
mod health;
mod imu;

pub use fusion::{FusionConfig, FusionHandle, FusionInputs, FusionService, FusionStats, NavState};
pub use gnss::{GnssHandle, GnssProcessor, GnssService, GnssStats, GnssStatus};
pub use health::{HealthService, HealthSnapshot};
pub use imu::{ImuHandle, ImuProcessor, ImuService, ImuStats, ImuStatus};

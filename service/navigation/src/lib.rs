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
pub mod observations;

#[cfg(feature = "probe")]
mod diagnostics {
    pub(super) fn configured(imus: usize, gnss: bool) {
        log::info!(
            target: "navigation::observations",
            "configured {imus} IMU channels, GNSS {}",
            if gnss { "enabled" } else { "disabled" }
        );
    }
}

#[cfg(not(feature = "probe"))]
mod diagnostics {
    pub(super) fn configured(_: usize, _: bool) {}
}

fn inbox_sink<T: Copy, E>(
    sender: messaging::Sender<E>,
    mut wrap: impl FnMut(T) -> Option<E>,
) -> impl FnMut(T) -> messaging::DeliveryResult<T> {
    use messaging::{DeliveryError, Rejected};
    use std::sync::mpsc::TrySendError;

    move |value| {
        let Some(event) = wrap(value) else {
            return Ok(());
        };
        sender.try_send(event).map_err(|error| Rejected {
            value,
            error: match error {
                TrySendError::Full(_) => DeliveryError::Full,
                TrySendError::Disconnected(_) => DeliveryError::Closed,
            },
        })
    }
}

#[cfg(test)]
mod transcript_tests;

pub use fusion::{FusionConfig, FusionHandle, FusionInputs, FusionService, FusionStats, NavState};
pub use gnss::{GnssHandle, GnssProcessor, GnssService, GnssStats, GnssStatus};
pub use health::{HealthService, HealthSnapshot};
pub use imu::{ImuHandle, ImuProcessor, ImuService, ImuStats, ImuStatus};

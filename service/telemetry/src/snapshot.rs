//! The sensor layout of the versioned navigation telemetry protocol.

use crate::ClockAnchor;
pub use navigation::observations::Reading;

/// Two IMU channels and one GNSS receiver, as defined by the telemetry wire
/// contract. Applications choose the actual devices and freshness deadlines.
/// Unconfigured sources have no live data and are represented as waiting.
#[derive(Clone, Copy)]
pub struct Snapshot {
    pub imu: [Reading<imu::ImuSample>; 2],
    pub gnss: Reading<gnss::GnssFix>,
    pub clock: Option<ClockAnchor>,
}

impl From<navigation::observations::Snapshot<2>> for Snapshot {
    fn from(snapshot: navigation::observations::Snapshot<2>) -> Self {
        Self {
            imu: snapshot.imu,
            gnss: snapshot.gnss.unwrap_or(Reading::Waiting),
            clock: None,
        }
    }
}

/// Adapt a natural one-IMU observation source without adding a subscription.
/// The second protocol slot remains waiting; no source is discarded.
impl From<navigation::observations::Snapshot<1>> for Snapshot {
    fn from(snapshot: navigation::observations::Snapshot<1>) -> Self {
        Self {
            imu: [snapshot.imu[0], Reading::Waiting],
            gnss: snapshot.gnss.unwrap_or(Reading::Waiting),
            clock: None,
        }
    }
}

/// Stable textual state names used by telemetry peers and human-readable output.
pub fn reading_status<T>(reading: &Reading<T>) -> &'static str {
    match reading {
        Reading::Waiting => "waiting",
        Reading::Live(_) => "live",
        Reading::Stale => "stale",
        Reading::Fault => "fault",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_gnss_has_no_live_data_and_does_not_panic() {
        let snapshot = Snapshot::from(navigation::observations::Snapshot {
            imu: [Reading::Waiting; 2],
            gnss: None,
        });
        assert_eq!(snapshot.gnss, Reading::Waiting);
        assert!(snapshot.clock.is_none());
    }
}

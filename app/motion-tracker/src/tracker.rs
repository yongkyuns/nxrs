pub use navigation::observations::Reading;
use navigation::observations::SensorFault;
use std::time::Duration;

pub const IMU_FRESH: Duration = Duration::from_millis(500);
pub const FIX_FRESH: Duration = Duration::from_secs(3);

/// The tracker supports two IMU channels and one GNSS receiver.
#[derive(Clone, Copy)]
pub struct Snapshot {
    pub imu: [Reading<imu::ImuSample>; 2],
    pub gnss: Reading<gnss::GnssFix>,
    pub(crate) clock: Option<crate::clock::ClockAnchor>,
}

impl From<navigation::observations::Snapshot<2>> for Snapshot {
    fn from(snapshot: navigation::observations::Snapshot<2>) -> Self {
        Self {
            imu: snapshot.imu,
            gnss: snapshot.gnss.expect("the tracker configures GNSS"),
            clock: None,
        }
    }
}

pub fn reading_status<T>(reading: &Reading<T>) -> &'static str {
    match reading {
        Reading::Waiting => "waiting",
        Reading::Live(_) => "live",
        Reading::Stale => "stale",
        Reading::Fault => "fault",
    }
}

pub fn report_fault(fault: SensorFault) {
    match fault {
        SensorFault::Imu(index, error) => log::warn!("motion-tracker IMU {}: {error:?}", index + 1),
        SensorFault::Gnss(error) => log::warn!("motion-tracker GNSS: {error:?}"),
    }
}

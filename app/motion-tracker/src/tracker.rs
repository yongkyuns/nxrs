use navigation::observations::SensorFault;
use std::time::Duration;
pub use telemetry::{reading_status, Reading, Snapshot};

pub const IMU_FRESH: Duration = Duration::from_millis(500);
pub const FIX_FRESH: Duration = Duration::from_secs(3);

pub fn report_fault(fault: SensorFault) {
    match fault {
        SensorFault::Imu(index, error) => log::warn!("motion-tracker IMU {}: {error:?}", index + 1),
        SensorFault::Gnss(error) => log::warn!("motion-tracker GNSS: {error:?}"),
    }
}

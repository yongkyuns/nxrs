pub(crate) use crate::communications::{reading_status, Snapshot};
pub use navigation::observations::Reading;
use navigation::observations::SensorFault;
use std::time::Duration;

pub const IMU_FRESH: Duration = Duration::from_millis(500);
pub const FIX_FRESH: Duration = Duration::from_secs(3);

pub fn status<T>(reading: Option<Reading<T>>) -> &'static str {
    reading.as_ref().map_or("unavailable", reading_status)
}

pub fn report_fault(fault: SensorFault) {
    match fault {
        SensorFault::Imu(index, error) => eprintln!("motion-tracker IMU {}: {error:?}", index + 1),
        SensorFault::Gnss(error) => eprintln!("motion-tracker GNSS: {error:?}"),
    }
}

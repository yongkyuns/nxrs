pub use navigation::observations::Reading;
use std::time::Duration;

pub type Snapshot = navigation::observations::Snapshot<2>;

pub const IMU_FRESH: Duration = Duration::from_millis(500);
pub const FIX_FRESH: Duration = Duration::from_secs(3);

pub fn status<T>(reading: Option<Reading<T>>) -> &'static str {
    match reading {
        None => "unavailable",
        Some(Reading::Waiting) => "waiting",
        Some(Reading::Live(_)) => "live",
        Some(Reading::Stale) => "stale",
        Some(Reading::Fault) => "fault",
    }
}

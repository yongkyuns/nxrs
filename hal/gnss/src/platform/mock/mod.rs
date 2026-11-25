//! Deterministic synthetic GNSS for architecture demonstrations.
#![forbid(unsafe_code)]
pub use crate::fixtures::devices;

use crate::session;
use crate::{DeviceError, Event, GnssFix, Session};
use acquisition::mock::Timer;

#[derive(Default)]
struct SyntheticGnss {
    sequence: u64,
    north_m: f32,
    east_m: f32,
}

impl SyntheticGnss {
    fn fix(&mut self, now_us: u64) -> Result<GnssFix, DeviceError> {
        self.sequence = self.sequence.saturating_add(1);
        self.north_m += 0.05;
        self.east_m += 0.80;
        Ok(GnssFix {
            sequence: self.sequence,
            timestamp_us: now_us,
            is_capture_time: false,
            receiver_utc_ms: None,
            latitude_deg: f64::from(self.north_m) / 111_319.490_793,
            longitude_deg: f64::from(self.east_m) / 111_319.490_793,
            speed_mps: 4.0,
            course_rad: Some(0.08),
            quality: None,
        })
    }
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Session + Send + 'static, DeviceError> {
    let mut model = SyntheticGnss::default();
    let timer = Timer::default();
    session::start(16_384, timer.clone(), (sink, ()), move |stop, emit| {
        timer
            .run(stop, std::time::Duration::from_millis(200), |now| {
                emit(Event::Fix(model.fix(now).expect("synthetic sample")))
            })
            .map_err(|_| DeviceError::Io)
    })
    .map_err(|_| DeviceError::Io)
}

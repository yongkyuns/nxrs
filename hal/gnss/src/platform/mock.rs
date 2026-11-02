//! Deterministic synthetic GNSS for architecture demonstrations.
#![forbid(unsafe_code)]

use crate::mock_acquisition::Session;
use crate::{DeviceError, Event, EventSink, Gnss, GnssFix};

#[derive(Default)]
struct SyntheticGnss {
    sequence: u64,
    north_m: f32,
    east_m: f32,
}

impl SyntheticGnss {
    fn fix(&mut self, now_ms: u64) -> Result<GnssFix, DeviceError> {
        self.sequence = self.sequence.saturating_add(1);
        self.north_m += 0.05;
        self.east_m += 0.80;
        Ok(GnssFix {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            latitude_deg: f64::from(self.north_m) / 111_319.490_793,
            longitude_deg: f64::from(self.east_m) / 111_319.490_793,
            speed_mps: 4.0,
            course_rad: Some(0.08),
        })
    }
}

impl Gnss for Session {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        Session::request_stop(self)
    }
    fn join(&mut self) -> Result<(), DeviceError> {
        Session::join(self)
    }
    fn is_finished(&self) -> bool {
        Session::is_finished(self)
    }
    fn dropped(&self) -> u32 {
        Session::dropped(self)
    }
}
pub fn open<S: EventSink<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Gnss + Send + 'static, DeviceError> {
    let mut model = SyntheticGnss::default();
    Session::start(sink, std::time::Duration::from_millis(200), move |now| {
        Event::Fix(model.fix(now).expect("synthetic sample"))
    })
}

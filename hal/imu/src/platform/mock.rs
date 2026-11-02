//! Deterministic synthetic IMU for architecture demonstrations.
#![forbid(unsafe_code)]

use crate::mock_acquisition::Session;
use crate::{DeviceError, Event, Imu, ImuSample};

#[derive(Default)]
struct SyntheticImu {
    sequence: u64,
}

impl SyntheticImu {
    fn sample(&mut self, now_ms: u64) -> Result<ImuSample, DeviceError> {
        self.sequence = self.sequence.saturating_add(1);
        let gyro_z = if self.sequence % 80 < 40 { 0.04 } else { -0.02 };
        Ok(ImuSample {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            sample_interval_us: 10_000,
            accel_mps2: [0.10, 0.0, 9.81],
            gyro_rps: [0.0, 0.0, gyro_z],
        })
    }
}

impl Imu for Session {
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
pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let mut model = SyntheticImu::default();
    Session::start(sink, std::time::Duration::from_millis(10), move |now| {
        Event::Sample(model.sample(now).expect("synthetic sample"))
    })
}

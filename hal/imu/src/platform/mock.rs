//! Deterministic synthetic IMU for architecture demonstrations.
#![forbid(unsafe_code)]

type Session = acquisition::mock::Session<DeviceError>;
use crate::{
    AccelRange, AppliedConfig, Capabilities, Config, DeviceError, Event, GyroRange, Imu, ImuSample,
};

const INTERVAL_US: u32 = 10_000;
const CAPABILITIES: Capabilities = Capabilities {
    rates_millihz: &[1_000_000_000 / INTERVAL_US],
    accel_ranges: &[AccelRange::G4],
    gyro_ranges: &[GyroRange::Dps2000],
};

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
            sample_interval_us: INTERVAL_US,
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
    Session::start(
        sink,
        std::time::Duration::from_micros(u64::from(INTERVAL_US)),
        DeviceError::Io,
        Event::Failed,
        move |now| Event::Sample(model.sample(now).expect("synthetic sample")),
    )
}

pub fn capabilities() -> Option<Capabilities> {
    Some(CAPABILITIES)
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    let applied = CAPABILITIES.select(config)?;
    open(sink).map(|session| (session, applied))
}

//! Deterministic synthetic IMU for architecture demonstrations.
#![forbid(unsafe_code)]

use peripheral::imu::select_rate;

pub(crate) use crate::probe::unsupported as open_probed;

type Session = acquisition::mock::Session<DeviceError>;
use crate::{AppliedConfig, Config, DeviceError, Event, Imu, ImuSample};

const INTERVAL_US: u32 = 10_000;

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

/// Preview the fixed source mode without starting acquisition.
pub fn resolve(config: Config) -> Result<AppliedConfig, DeviceError> {
    let (_, applied) = select_rate(
        config,
        [1_000_000_000 / INTERVAL_US],
        core::convert::identity,
    )?;
    if config.accel_range_g != 4 || config.gyro_range_dps != 2000 {
        return Err(DeviceError::Unsupported);
    }
    Ok(applied)
}

pub fn open_configured<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    config: Config,
    sink: S,
) -> Result<(impl Imu + Send + 'static, AppliedConfig), DeviceError> {
    let applied = resolve(config)?;
    open(sink).map(|session| (session, applied))
}

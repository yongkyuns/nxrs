//! Deterministic synthetic IMU for architecture demonstrations.
#![forbid(unsafe_code)]
#[path = "../device_state.rs"]
mod device_state;

use peripheral::imu::select_rate;

pub(crate) use crate::probe::unsupported as open_probed;

type MockSession = acquisition::mock::Session<DeviceError>;
use crate::{AppliedConfig, Config, DeviceError, Event, ImuSample, Session};

const INTERVAL_US: u32 = 10_000;

#[derive(Default)]
struct SyntheticImu {
    sequence: u64,
}

impl SyntheticImu {
    fn sample(&mut self, now_ms: u64) -> ImuSample {
        self.sequence = self.sequence.saturating_add(1);
        let gyro_z = if self.sequence % 80 < 40 { 0.04 } else { -0.02 };
        ImuSample {
            sequence: self.sequence,
            timestamp_ms: now_ms,
            sample_interval_us: INTERVAL_US,
            accel_mps2: [0.10, 0.0, 9.81],
            gyro_rps: [0.0, 0.0, gyro_z],
        }
    }
}

impl Session for MockSession {
    fn request_stop(&mut self) -> Result<(), DeviceError> {
        MockSession::request_stop(self)
    }
    fn join(&mut self) -> Result<(), DeviceError> {
        MockSession::join(self)
    }
    fn is_finished(&self) -> bool {
        MockSession::is_finished(self)
    }
    fn dropped(&self) -> u32 {
        MockSession::dropped(self)
    }
}

struct SelectedDevice {
    id: &'static str,
    state: device_state::DeviceState,
}
static PRIMARY: SelectedDevice = SelectedDevice {
    id: "primary",
    state: device_state::DeviceState::new(),
};
static SECONDARY: SelectedDevice = SelectedDevice {
    id: "secondary",
    state: device_state::DeviceState::new(),
};
impl crate::api::Sealed for SelectedDevice {}
impl crate::Device for SelectedDevice {
    fn id(&self) -> &'static str {
        self.id
    }
    fn default_config(&self) -> Result<Config, DeviceError> {
        Ok(Config {
            min_rate_millihz: 1_000_000_000 / INTERVAL_US,
            max_rate_millihz: 1_000_000_000 / INTERVAL_US,
            accel_range_g: 4,
            gyro_range_dps: 2000,
            max_delivery_interval_us: INTERVAL_US,
        })
    }
    fn resolve(&self, config: Config) -> Result<AppliedConfig, DeviceError> {
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
    fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
        &'static self,
        config: Config,
        sink: S,
    ) -> Result<(impl Session + Send + 'static, AppliedConfig), DeviceError> {
        let applied = self.resolve(config)?;
        let mut retained = (sink, self.state.begin_use()?);
        let mut model = SyntheticImu::default();
        MockSession::start(
            move |event| {
                let retained = &mut retained; // The sink is destroyed before the device becomes available.
                (retained.0)(event)
            },
            std::time::Duration::from_micros(u64::from(INTERVAL_US)),
            DeviceError::Io,
            Event::Failed,
            move |now| Event::Sample(model.sample(now)),
        )
        .map(|session| (session, applied))
    }
}
pub mod devices {
    pub fn primary() -> &'static impl crate::Device {
        &super::PRIMARY
    }
    /// Second independent scripted device in the explicit mock fixture.
    pub fn secondary() -> &'static impl crate::Device {
        &super::SECONDARY
    }
}
pub use devices::primary;

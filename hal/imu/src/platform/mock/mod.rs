//! Deterministic synthetic IMU for architecture demonstrations.
#![forbid(unsafe_code)]
use crate::device_state;

use peripheral::imu::select_rate;

use crate::session;
use crate::{AppliedConfig, Config, DeviceError, Event, ImuSample, Session};
use acquisition::mock::Timer;

const INTERVAL_US: u32 = 10_000;

#[derive(Default)]
struct SyntheticImu {
    sequence: u64,
}

impl SyntheticImu {
    fn sample(&mut self, now_us: u64) -> ImuSample {
        self.sequence = self.sequence.saturating_add(1);
        let gyro_z = if self.sequence % 80 < 40 { 0.04 } else { -0.02 };
        ImuSample {
            sequence: self.sequence,
            timestamp_us: now_us,
            received_us: now_us,
            is_capture_time: false,
            sample_interval_us: INTERVAL_US,
            accel_mps2: [0.10, 0.0, 9.81],
            gyro_rps: [0.0, 0.0, gyro_z],
        }
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
        let delivery = (sink, self.state.begin_use()?);
        let mut model = SyntheticImu::default();
        let timer = Timer::default();
        session::start(16_384, timer.clone(), delivery, move |stop, emit| {
            timer
                .run(
                    stop,
                    std::time::Duration::from_micros(u64::from(INTERVAL_US)),
                    |now| emit(Event::Sample(model.sample(now))),
                )
                .map_err(|_| DeviceError::Io)
        })
        .map(|session| (session, applied))
        .map_err(|_| DeviceError::Io)
    }
}
pub mod devices {
    pub fn primary_imu() -> &'static impl crate::Device {
        &super::PRIMARY
    }
    /// Second independent scripted device in the explicit mock fixture.
    pub fn secondary() -> &'static impl crate::Device {
        &super::SECONDARY
    }
}

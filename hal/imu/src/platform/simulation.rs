//! Normalize observations from the profile's shared simulation session.
#[path = "../configuration.rs"]
mod configuration;

pub(crate) use crate::probe::unsupported as open_probed;

#[path = "../session.rs"]
mod session;
use crate::{AppliedConfig, Config, DeviceError, Event, Imu, ImuSample};
use nxrs_simulation::{self as simulation, runtime, CancelHandle, Error, Observation, Source};
use session::Session;
use std::io;

include!(concat!(env!("OUT_DIR"), "/simulation_config.rs"));

const INTERVAL_US: u32 = simulation::DEFAULT_IMU_PERIOD_US as u32;

struct Cancel(CancelHandle);

impl acquisition::Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        self.0.cancel();
        Ok(())
    }
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Imu + Send + 'static, DeviceError> {
    let endpoint =
        runtime::acquire(SIM_SEED, SIM_SOURCES, Source::Imu).map_err(|error| match error {
            runtime::AcquireError::Engine(Error::Busy) | runtime::AcquireError::ConflictingSpec => {
                DeviceError::Busy
            }
            runtime::AcquireError::Engine(Error::SourceNotConfigured) => DeviceError::Unsupported,
            _ => DeviceError::Io,
        })?;
    let cancel = Cancel(endpoint.cancel_handle());
    Session::start(65_536, cancel, sink, move |stop, emit| {
        while !stop.load(core::sync::atomic::Ordering::Acquire) {
            let event = match endpoint.recv() {
                Ok(event) => event,
                Err(Error::Cancelled) if stop.load(core::sync::atomic::Ordering::Acquire) => break,
                Err(_) => return Err(DeviceError::Io),
            };
            let Observation::Imu(sample) = event.observation else {
                return Err(DeviceError::InvalidData);
            };
            if !emit(Event::Sample(ImuSample {
                sequence: event.key.sequence.saturating_add(1),
                timestamp_ms: event.key.time_us / 1_000,
                sample_interval_us: INTERVAL_US,
                accel_mps2: sample
                    .accel_ug
                    .map(|value| value as f32 * (9.80665 / 1_000_000.0)),
                gyro_rps: sample
                    .gyro_udps
                    .map(|value| (value as f32 / 1_000_000.0).to_radians()),
            })) {
                break;
            }
        }
        Ok(())
    })
}

/// Preview the fixed source mode without starting acquisition.
pub fn resolve(config: Config) -> Result<AppliedConfig, DeviceError> {
    let (_, applied) = configuration::select_rate(
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

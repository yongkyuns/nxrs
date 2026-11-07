//! Normalize observations from the profile's shared simulation session.
use crate::{
    session::Session, AccelRange, AppliedConfig, Capabilities, Config, DeviceError, Event,
    GyroRange, Imu, ImuSample,
};
use nxrs_simulation::{self as simulation, runtime, CancelHandle, Error, Observation, Source};
use std::io;

include!(concat!(env!("OUT_DIR"), "/simulation_config.rs"));

const INTERVAL_US: u32 = simulation::DEFAULT_IMU_PERIOD_US as u32;
const RATE: u32 = 1_000_000_000 / INTERVAL_US;
const CAPABILITIES: Capabilities = Capabilities {
    rates_millihz: &[RATE],
    accel_ranges: &[AccelRange::G4],
    gyro_ranges: &[GyroRange::Dps2000],
};

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

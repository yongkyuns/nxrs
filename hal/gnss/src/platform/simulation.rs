//! Normalize fixes from the profile's shared simulation session.
use crate::{session::WorkerSession, DeviceError, Event, GnssFix, Session};
use nxrs_simulation::{self as simulation, runtime, CancelHandle, Error, Observation, Source};
use std::io;

include!(concat!(env!("OUT_DIR"), "/simulation_config.rs"));

struct Cancel(CancelHandle);

impl acquisition::Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        self.0.cancel();
        Ok(())
    }
}

pub fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<impl Session + Send + 'static, DeviceError> {
    let endpoint =
        runtime::acquire(SIM_SEED, SIM_SOURCES, Source::Gnss).map_err(|error| match error {
            runtime::AcquireError::Engine(Error::Busy) | runtime::AcquireError::ConflictingSpec => {
                DeviceError::Busy
            }
            runtime::AcquireError::Engine(Error::SourceNotConfigured) => DeviceError::Unsupported,
            _ => DeviceError::Io,
        })?;
    let cancel = Cancel(endpoint.cancel_handle());
    WorkerSession::start(65_536, cancel, sink, move |stop, emit| {
        while !stop.load(core::sync::atomic::Ordering::Acquire) {
            let event = match endpoint.recv() {
                Ok(event) => event,
                Err(Error::Cancelled) if stop.load(core::sync::atomic::Ordering::Acquire) => break,
                Err(_) => return Err(DeviceError::Io),
            };
            let Observation::Gnss(fix) = event.observation else {
                return Err(DeviceError::InvalidData);
            };
            if !emit(Event::Fix(GnssFix {
                sequence: event.key.sequence.saturating_add(1),
                timestamp_ms: event.key.time_us / 1_000,
                latitude_deg: f64::from(fix.latitude_e7) / 10_000_000.0,
                longitude_deg: f64::from(fix.longitude_e7) / 10_000_000.0,
                speed_mps: fix.speed_mm_s as f32 / 1_000.0,
                course_rad: fix.course_mrad.map(|value| value as f32 / 1_000.0),
            })) {
                break;
            }
        }
        Ok(())
    })
}

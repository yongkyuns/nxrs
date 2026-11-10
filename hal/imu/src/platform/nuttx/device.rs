//! Resource opening, acquisition and lifetime of a normalized IMU.
use super::device_state::{DeviceState, DeviceUseGuard};
use super::{gpio_resource, signal::Signal, trace::Trace};
use crate::{AppliedConfig, Config, DeviceError, Event, ImuSample};
use core::sync::atomic::{AtomicBool, Ordering};
use peripheral::imu::{Buffering, Error, Imu};
use std::{
    fs::File,
    time::{Duration, Instant},
};

pub(super) type Irq = gpio_resource::Interrupt<&'static DeviceState>;

pub(super) fn gpio_error(error: gpio_resource::Error) -> DeviceError {
    match error {
        gpio_resource::Error::Unsupported => DeviceError::Unsupported,
        gpio_resource::Error::Io => DeviceError::Io,
    }
}

pub(super) fn open_irq(path: &str, state: &'static DeviceState) -> Result<Irq, DeviceError> {
    gpio_resource::Interrupt::open_rising_with_context(path, state, |state| state.mark_faulted())
        .map_err(gpio_error)
}

pub(super) struct Device<S: Imu> {
    sensor: S,
    applied: AppliedConfig,
    signal: Signal,
    trace: Trace,
    cleaned: bool,
    use_guard: DeviceUseGuard,
}

/// The current acquisition loop requires FIFO retention. Latest-value sensors
/// need their own explicit loss/readiness policy before this HAL admits them.
pub(super) fn resolve<S: Imu>(config: Config) -> Result<(S::Settings, AppliedConfig), DeviceError> {
    if S::BUFFERING != Buffering::Fifo {
        return Err(DeviceError::Unsupported);
    }
    S::resolve(config).map_err(Into::into)
}

impl<S: Imu> Device<S> {
    /// Validate, reserve and open resources before initializing the sensor.
    pub(super) fn open<R>(
        config: Config,
        irq_path: Option<&str>,
        trace: Trace,
        state: &'static DeviceState,
        open: impl FnOnce() -> Result<R, DeviceError>,
        initialize: impl FnOnce(R, S::Settings) -> Result<S, Error>,
    ) -> Result<Self, DeviceError> {
        let (settings, applied) = resolve::<S>(config)?;
        let use_guard = state.begin_use()?;
        // A configured IRQ failure must never select the periodic mode.
        let irq = irq_path.map(|path| open_irq(path, state)).transpose()?;
        let resources = open()?;
        let sensor = initialize(resources, settings).inspect_err(|_| {
            // An interrupted configuration may have left the sensor active.
            state.mark_faulted();
        })?;
        let period = Duration::from_micros(u64::from(applied.delivery_interval_us));
        Ok(Self {
            sensor,
            applied,
            signal: Signal::new(irq, period),
            trace,
            cleaned: false,
            use_guard,
        })
    }

    /// Apply optional sensor setup before publication, retaining cleanup on failure.
    pub(super) fn prepare(
        mut self,
        configure: impl FnOnce(&mut S) -> Result<(), peripheral::imu::Error>,
    ) -> Result<Self, DeviceError> {
        if let Err(error) = configure(&mut self.sensor) {
            return Err(self.cleanup().err().unwrap_or(error.into()));
        }
        Ok(self)
    }

    pub(super) fn applied_config(&self) -> AppliedConfig {
        self.applied
    }

    pub(super) fn run(
        &mut self,
        stop: &AtomicBool,
        wake: &File,
        emit: &mut dyn FnMut(Event) -> bool,
    ) -> Result<(), DeviceError> {
        let origin = Instant::now();
        let mut sequence = 0u64;
        let sample_interval_us = 1_000_000_000u32.div_ceil(self.applied.rate_millihz);
        let result = (|| {
            while !stop.load(Ordering::Acquire) {
                let Some(ready) = self.signal.wait(wake)? else {
                    break;
                };
                self.trace.wake(ready.periodic_deadline);
                let now = origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
                let drained = self.sensor.read_available(|sample| {
                    sequence = sequence.saturating_add(1);
                    emit(Event::Sample(ImuSample {
                        sequence,
                        timestamp_ms: now,
                        sample_interval_us,
                        accel_mps2: sample.accel_mps2,
                        gyro_rps: sample.gyro_rps,
                    }))
                });
                self.trace.drain(matches!(&drained, Ok(Some(_))));
                let Some(count) = drained? else {
                    break;
                };
                self.signal.drained(count)?;
            }
            Ok(())
        })();
        self.cleanup().and(result)
    }

    fn cleanup(&mut self) -> Result<(), DeviceError> {
        if core::mem::replace(&mut self.cleaned, true) {
            return Ok(());
        }
        let stopped = self.sensor.stop().map_err(DeviceError::from);
        let detached = self.signal.close();
        if stopped.is_err() || detached.is_err() {
            self.use_guard.mark_faulted();
        }
        stopped.and(detached)
    }
}

impl<S: Imu> Drop for Device<S> {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use peripheral::imu::{Error, Measurement};

    struct Latest;
    impl Imu for Latest {
        const DEFAULT: Config = Config {
            min_rate_millihz: 100_000,
            max_rate_millihz: 100_000,
            accel_range_g: 4,
            gyro_range_dps: 2000,
            max_delivery_interval_us: 10_000,
        };
        const BUFFERING: Buffering = Buffering::Latest;
        type Settings = ();
        fn resolve(_: Config) -> Result<((), AppliedConfig), Error> {
            panic!("unsupported mode")
        }
        fn read_available(
            &mut self,
            _: impl FnMut(Measurement) -> bool,
        ) -> Result<Option<usize>, Error> {
            panic!("unsupported mode")
        }
        fn stop(&mut self) -> Result<(), Error> {
            panic!("unsupported mode")
        }
    }

    #[test]
    fn latest_value_device_cannot_claim_fifo_acquisition() {
        assert_eq!(
            resolve::<Latest>(Latest::DEFAULT),
            Err(DeviceError::Unsupported)
        );
    }
}

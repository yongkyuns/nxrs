//! Physical device ownership. Construction and cleanup stay in task context.
use super::{
    construction::Construct, gpio_resource, observed_bus::Bus, signal::Signal, trace::Trace, OWNED,
};
use crate::{AppliedConfig, Config, DeviceError, Event, ImuSample};
use core::sync::atomic::{AtomicBool, Ordering};
use embedded_hal::delay::DelayNs;
use peripheral::imu::{Buffering, Imu};
use std::{
    fs::File,
    time::{Duration, Instant},
};

pub(super) type Irq = gpio_resource::Interrupt<fn()>;

fn quarantine() {
    OWNED.store(2, Ordering::Release);
}

pub(super) fn gpio_error(error: gpio_resource::Error) -> DeviceError {
    match error {
        gpio_resource::Error::Unsupported => DeviceError::Unsupported,
        gpio_resource::Error::Io => DeviceError::Io,
    }
}

pub(super) fn open_irq(path: &str) -> Result<Irq, DeviceError> {
    gpio_resource::Interrupt::open_rising(path, quarantine as fn()).map_err(gpio_error)
}

pub(super) struct Binding<'a> {
    pub(super) path: &'a str,
    pub(super) address: u8,
    pub(super) frequency: u32,
    pub(super) irq_path: Option<&'a str>,
}

struct Lease;

impl Lease {
    fn take() -> Result<Self, DeviceError> {
        OWNED
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|state| {
                if state == 1 {
                    DeviceError::Busy
                } else {
                    DeviceError::Io
                }
            })?;
        Ok(Self)
    }
}

impl Drop for Lease {
    fn drop(&mut self) {
        let _ = OWNED.compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}

pub(super) struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) struct Device<S: Construct> {
    sensor: S,
    applied: AppliedConfig,
    signal: Signal,
    trace: Trace,
    cleaned: bool,
    _lease: Lease,
}

/// The current acquisition loop requires FIFO retention. Latest-value sensors
/// need their own explicit loss/readiness policy before this HAL admits them.
pub(super) fn resolve<S: Imu>(config: Config) -> Result<(S::Settings, AppliedConfig), DeviceError> {
    if S::BUFFERING != Buffering::Fifo {
        return Err(DeviceError::Unsupported);
    }
    S::resolve(config).map_err(Into::into)
}

impl<S: Construct> Device<S> {
    pub(super) fn acquire(
        binding: Binding<'_>,
        config: Config,
        trace: Trace,
    ) -> Result<Self, DeviceError> {
        let (settings, applied) = resolve::<S>(config)?;
        let lease = Lease::take()?;
        // An absent GPIO uses an ODR-derived wake. Failure of a configured IRQ
        // never changes acquisition mode.
        let irq = binding.irq_path.map(open_irq).transpose()?;
        let bus = Bus::open(binding.path, binding.frequency, trace.clone())?;
        let sensor = S::open(bus, binding.address, settings).inspect_err(|_| {
            // An interrupted configuration may have left the sensor active.
            OWNED.store(2, Ordering::Release);
        })?;
        let period = Duration::from_micros(u64::from(applied.delivery_interval_us));
        let signal = Signal::new(irq, period);
        let mut device = Self {
            sensor,
            applied,
            signal,
            trace,
            cleaned: false,
            _lease: lease,
        };
        if device.trace.sensor_reference() {
            if let Err(error) = device.sensor.enable_sensor_reference() {
                return Err(device.cleanup().err().unwrap_or(error.into()));
            }
        }
        Ok(device)
    }

    pub(super) fn applied_config(&self) -> AppliedConfig {
        self.applied
    }

    pub(super) fn run(
        mut self,
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
            OWNED.store(2, Ordering::Release);
        }
        stopped.and(detached)
    }
}

impl<S: Construct> Drop for Device<S> {
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
        const DEFAULT: Config = <nxrs_bmi270::Bmi270<Bus, Delay> as Imu>::DEFAULT;
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

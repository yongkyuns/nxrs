//! Physical device ownership. Construction and cleanup stay in task context.
use super::{i2c::Bus, irq::Irq, signal::Signal, trace::Trace, OWNED};
use crate::{sensors::Sensor, AppliedConfig, Config, DeviceError, Event};
use core::sync::atomic::{AtomicBool, Ordering};
use embedded_hal::delay::DelayNs;
use std::{
    fs::File,
    time::{Duration, Instant},
};

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

pub(super) struct Device<S: Sensor<Bus, Delay>> {
    sensor: S,
    signal: Signal,
    trace: Trace,
    cleaned: bool,
    _lease: Lease,
}

impl<S: Sensor<Bus, Delay>> Device<S> {
    pub(super) fn acquire(
        binding: Binding<'_>,
        config: Config,
        trace: Trace,
    ) -> Result<Self, DeviceError> {
        let applied = S::CAPABILITIES.select(config)?;
        let lease = Lease::take()?;
        // An absent GPIO uses an ODR-derived wake. Failure of a configured IRQ
        // never changes acquisition mode.
        let irq = binding.irq_path.map(Irq::open).transpose()?;
        let bus = Bus::open(binding.path, binding.frequency, trace.clone())?;
        let sensor = S::open(bus, binding.address, Delay, applied).inspect_err(|_| {
            // An interrupted configuration may have left the sensor active.
            OWNED.store(2, Ordering::Release);
        })?;
        let period = Duration::from_micros(u64::from(applied.delivery_interval_us));
        let signal = Signal::new(irq, period);
        let mut device = Self {
            sensor,
            signal,
            trace,
            cleaned: false,
            _lease: lease,
        };
        if device.trace.sensor_reference() {
            if let Err(error) = device.sensor.enable_sensor_reference() {
                return Err(device.cleanup().err().unwrap_or(error));
            }
        }
        Ok(device)
    }

    pub(super) fn applied_config(&self) -> AppliedConfig {
        self.sensor.applied_config()
    }

    pub(super) fn run(
        mut self,
        stop: &AtomicBool,
        wake: &File,
        emit: &mut dyn FnMut(Event) -> bool,
    ) -> Result<(), DeviceError> {
        let origin = Instant::now();
        let result = (|| {
            while !stop.load(Ordering::Acquire) {
                let Some(ready) = self.signal.wait(wake)? else {
                    break;
                };
                self.trace.wake(ready.periodic_deadline);
                let now = origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
                let drained = self.sensor.drain(now, |sample| emit(Event::Sample(sample)));
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
        let stopped = self.sensor.stop();
        let detached = self.signal.close();
        if stopped.is_err() || detached.is_err() {
            OWNED.store(2, Ordering::Release);
        }
        stopped.and(detached)
    }
}

impl<S: Sensor<Bus, Delay>> Drop for Device<S> {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

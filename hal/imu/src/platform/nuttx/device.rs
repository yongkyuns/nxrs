//! Physical device ownership. Construction and cleanup stay in task context.
use super::{i2c::Bus, irq::Irq, signal::Signal, OWNED};
use crate::{sensors::bmi270::Sensor, DeviceError, Event};
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

struct Delay;

impl DelayNs for Delay {
    fn delay_ns(&mut self, ns: u32) {
        std::thread::sleep(Duration::from_nanos(u64::from(ns)));
    }
}

pub(super) struct Device {
    sensor: Sensor<Bus, Delay>,
    signal: Signal,
    cleaned: bool,
    _lease: Lease,
}

impl Device {
    pub(super) fn acquire(binding: Binding<'_>) -> Result<Self, DeviceError> {
        let lease = Lease::take()?;
        // An absent GPIO uses an ODR-derived wake. Failure of a configured IRQ
        // never changes acquisition mode.
        let irq = binding.irq_path.map(Irq::open).transpose()?;
        let bus = Bus::open(binding.path, binding.frequency)?;
        let sensor = Sensor::open(bus, binding.address, Delay).inspect_err(|_| {
            // An interrupted configuration may have left the sensor active.
            OWNED.store(2, Ordering::Release);
        })?;
        let signal = Signal::new(irq, sensor.sample_period());
        Ok(Self {
            sensor,
            signal,
            cleaned: false,
            _lease: lease,
        })
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
                if !self.signal.wait(wake)? {
                    break;
                }
                let now = origin.elapsed().as_millis().try_into().unwrap_or(u64::MAX);
                let Some(count) = self
                    .sensor
                    .drain(now, |sample| emit(Event::Sample(sample)))?
                else {
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

impl Drop for Device {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

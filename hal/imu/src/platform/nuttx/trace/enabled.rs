//! Instrumented tracing, compiled only by the explicit `probe` feature.
use crate::{probe::Timing, DeviceError};
use std::{sync::Arc, time::Instant};

#[derive(Clone, Default)]
pub(crate) struct Trace {
    observer: Option<Arc<dyn Fn(Timing) + Send + Sync>>,
    sensor_reference: bool,
}

impl Trace {
    pub(crate) fn new(
        observe: impl Fn(Timing) + Send + Sync + 'static,
        sensor_reference: bool,
    ) -> Result<Self, DeviceError> {
        Ok(Self {
            observer: Some(Arc::new(observe)),
            sensor_reference,
        })
    }

    pub(crate) fn sensor_reference(&self) -> bool {
        self.sensor_reference
    }

    #[inline]
    pub(crate) fn wake(&self, periodic_deadline: Option<Instant>) {
        if let Some(observe) = &self.observer {
            observe(Timing::Wake {
                at: Instant::now(),
                periodic_deadline,
            });
        }
    }

    #[inline]
    pub(crate) fn ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
        if let Some(observe) = &self.observer {
            let start = Instant::now();
            let result = call();
            observe(Timing::Ioctl {
                start,
                end: Instant::now(),
            });
            return result;
        }
        call()
    }

    #[inline]
    pub(crate) fn drain(&self, complete: bool) {
        if let Some(observe) = &self.observer {
            observe(Timing::Drain {
                at: Instant::now(),
                complete,
            });
        }
    }
}

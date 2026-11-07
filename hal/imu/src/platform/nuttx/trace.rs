//! Inactive in the ordinary HAL. Timing runs only with an explicit observer.
use std::time::Instant;

#[cfg(feature = "probe")]
use {crate::probe::Timing, std::sync::Arc};

#[derive(Clone, Default)]
pub(super) struct Trace {
    #[cfg(feature = "probe")]
    observer: Option<Arc<dyn Fn(Timing) + Send + Sync>>,
    #[cfg(feature = "probe")]
    sensor_reference: bool,
}

impl Trace {
    #[cfg(feature = "probe")]
    pub(super) fn new(observe: impl Fn(Timing) + Send + Sync + 'static) -> Self {
        Self {
            observer: Some(Arc::new(observe)),
            sensor_reference: false,
        }
    }

    #[cfg(feature = "probe")]
    #[allow(
        dead_code,
        reason = "Only the NuttX sensor-reference probe uses this option."
    )]
    pub(super) fn with_sensor_reference(mut self, enabled: bool) -> Self {
        self.sensor_reference = enabled;
        self
    }

    pub(super) fn sensor_reference(&self) -> bool {
        #[cfg(feature = "probe")]
        {
            self.sensor_reference
        }
        #[cfg(not(feature = "probe"))]
        {
            false
        }
    }

    #[inline]
    pub(super) fn wake(&self, _periodic_deadline: Option<Instant>) {
        #[cfg(feature = "probe")]
        if let Some(observe) = &self.observer {
            observe(Timing::Wake {
                at: Instant::now(),
                periodic_deadline: _periodic_deadline,
            });
        }
    }

    #[inline]
    pub(super) fn ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
        #[cfg(feature = "probe")]
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
    pub(super) fn drain(&self, _complete: bool) {
        #[cfg(feature = "probe")]
        if let Some(observe) = &self.observer {
            observe(Timing::Drain {
                at: Instant::now(),
                complete: _complete,
            });
        }
    }
}

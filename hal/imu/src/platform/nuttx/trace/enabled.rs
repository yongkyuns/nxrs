//! Instrumented tracing, compiled only by the explicit `probe` feature.
use crate::{probe::Timing, DeviceError};
use embedded_hal::{i2c, spi};
use std::{sync::Arc, time::Instant};

/// The same opt-in observer wraps either resource's transaction contract.
pub struct Observed<T> {
    inner: T,
    trace: Trace,
}
impl<T: i2c::I2c> i2c::ErrorType for Observed<T> {
    type Error = T::Error;
}
impl<T: i2c::I2c> i2c::I2c for Observed<T> {
    fn transaction(
        &mut self,
        address: u8,
        operations: &mut [i2c::Operation<'_>],
    ) -> Result<(), Self::Error> {
        self.trace
            .ioctl(|| self.inner.transaction(address, operations))
    }
}
impl<T: spi::SpiDevice> spi::ErrorType for Observed<T> {
    type Error = T::Error;
}
impl<T: spi::SpiDevice> spi::SpiDevice for Observed<T> {
    fn transaction(
        &mut self,
        operations: &mut [spi::Operation<'_, u8>],
    ) -> Result<(), Self::Error> {
        self.trace.ioctl(|| self.inner.transaction(operations))
    }
}

#[derive(Clone, Default)]
pub struct Trace {
    observer: Option<Arc<dyn Fn(Timing) + Send + Sync>>,
}

impl Trace {
    pub fn observe<T>(self, inner: T) -> Observed<T> {
        Observed { inner, trace: self }
    }
    pub fn new(observe: impl Fn(Timing) + Send + Sync + 'static) -> Result<Self, DeviceError> {
        Ok(Self {
            observer: Some(Arc::new(observe)),
        })
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

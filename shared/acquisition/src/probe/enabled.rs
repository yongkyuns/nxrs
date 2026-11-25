//! Instrumented tracing, compiled only by the explicit `probe` feature.
use super::Timing;
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
    /// Returns `None` when instrumentation was not compiled with `probe`.
    pub fn new(observe: impl Fn(Timing) + Send + Sync + 'static) -> Option<Self> {
        Some(Self {
            observer: Some(Arc::new(observe)),
        })
    }

    #[inline]
    pub fn wake(&self, periodic_deadline: Option<Instant>) {
        if let Some(observe) = &self.observer {
            observe(Timing::Wake {
                at: Instant::now(),
                periodic_deadline,
            });
        }
    }

    #[inline]
    fn ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
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
    pub fn drain(&self, complete: bool) {
        if let Some(observe) = &self.observer {
            observe(Timing::Drain {
                at: Instant::now(),
                complete,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    struct FailedBus;
    impl i2c::ErrorType for FailedBus {
        type Error = i2c::ErrorKind;
    }
    impl i2c::I2c for FailedBus {
        fn transaction(
            &mut self,
            address: u8,
            operations: &mut [i2c::Operation<'_>],
        ) -> Result<(), Self::Error> {
            assert_eq!(address, 0x42);
            assert!(matches!(operations, [i2c::Operation::Write([1, 2])]));
            Err(i2c::ErrorKind::ArbitrationLoss)
        }
    }
    impl spi::ErrorType for FailedBus {
        type Error = spi::ErrorKind;
    }
    impl spi::SpiDevice for FailedBus {
        fn transaction(
            &mut self,
            operations: &mut [spi::Operation<'_, u8>],
        ) -> Result<(), Self::Error> {
            assert!(matches!(operations, [spi::Operation::Write([3, 4])]));
            Err(spi::ErrorKind::ChipSelectFault)
        }
    }

    #[test]
    fn both_bus_observers_forward_operations_and_report_failed_transactions() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let captured = events.clone();
        let trace = Trace::new(move |event| captured.lock().unwrap().push(event)).unwrap();
        let mut bus = trace.observe(FailedBus);
        assert_eq!(
            i2c::I2c::write(&mut bus, 0x42, &[1, 2]),
            Err(i2c::ErrorKind::ArbitrationLoss)
        );
        assert_eq!(
            spi::SpiDevice::write(&mut bus, &[3, 4]),
            Err(spi::ErrorKind::ChipSelectFault)
        );
        let events = events.lock().unwrap();
        assert_eq!(events.len(), 2);
        assert!(events
            .iter()
            .all(|event| matches!(event, Timing::Ioctl { start, end } if start <= end)));
    }
}

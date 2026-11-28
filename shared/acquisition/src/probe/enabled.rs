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
    source: Option<Source>,
}

#[derive(Clone)]
struct Source {
    capability: &'static str,
    id: &'static str,
    fingerprint: u64,
    plain: bool,
}

impl Source {
    fn new(capability: &'static str, id: &'static str) -> Self {
        // Compute the fallback identity once at session setup. Recording work
        // stays bounded even if a product has an unusually long device name.
        let bytes = capability.bytes().chain([0]).chain(id.bytes());
        let fingerprint = bytes.fold(0xcbf29ce484222325u64, |hash, byte| {
            (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
        });
        let plain = capability.bytes().chain(id.bytes()).all(label_byte);
        Self {
            capability,
            id,
            fingerprint,
            plain,
        }
    }

    fn label(&self, stage: &str) -> ([u8; 96], usize) {
        let mut label = [0u8; 96];
        let mut used = 0;
        let mut append = |text: &str, limit: usize| {
            for byte in text.bytes().take(limit) {
                label[used] = if label_byte(byte) { byte } else { b'_' };
                used += 1;
            }
        };
        if self.plain
            && stage.len() <= 32
            && stage.bytes().all(label_byte)
            && self
                .capability
                .len()
                .saturating_add(self.id.len())
                .saturating_add(stage.len())
                <= 94
        {
            for text in [self.capability, "/", self.id, "/", stage] {
                append(text, text.len());
            }
        } else {
            append(self.capability, 12);
            append("/", 1);
            append(self.id, 30);
            append("~", 1);
            // Preserve the stage and a stable source suffix instead of silently
            // merging long or sanitized device identifiers. The tilde makes
            // the shortened spelling visible in the viewer.
            for shift in (0..16).rev() {
                label[used] =
                    b"0123456789abcdef"[((self.fingerprint >> (shift * 4)) & 15) as usize];
                used += 1;
            }
            label[used] = b'/';
            used += 1;
            for byte in stage.bytes().take(32) {
                label[used] = if label_byte(byte) { byte } else { b'_' };
                used += 1;
            }
        }
        (label, used)
    }
}

fn label_byte(byte: u8) -> bool {
    byte.is_ascii_graphic() && byte != b'|'
}

impl Trace {
    pub fn observe<T>(self, inner: T) -> Observed<T> {
        Observed { inner, trace: self }
    }
    /// Returns `None` when instrumentation was not compiled with `probe`.
    pub fn new(observe: impl Fn(Timing) + Send + Sync + 'static) -> Option<Self> {
        Some(Self {
            observer: Some(Arc::new(observe)),
            source: None,
        })
    }

    /// Record a canonical device's task-side work without allocating an observer.
    /// The native note filter still controls whether records are retained.
    pub fn for_source(capability: &'static str, id: &'static str) -> Self {
        Self {
            observer: None,
            source: Some(Source::new(capability, id)),
        }
    }

    fn record(&self, edge: u8, stage: &str) {
        if let Some(source) = &self.source {
            let (label, used) = source.label(stage);
            super::recorder::record(edge, &label[..used]);
        }
    }

    /// Record one semantic observation in the current acquisition task.
    /// Labels are bounded and the native recorder controls admission.
    #[inline]
    pub fn instant(&self, stage: &str) {
        self.record(b'I', stage);
    }

    /// Observe a synchronous operation. End notes also run during host unwinding.
    /// On panic-abort targets an unfinished scope remains explicitly incomplete.
    pub fn scope<T>(&self, stage: &str, call: impl FnOnce() -> T) -> T {
        struct End<'a>(&'a Trace, &'a str);
        impl Drop for End<'_> {
            fn drop(&mut self) {
                self.0.record(b'E', self.1);
            }
        }
        self.record(b'B', stage);
        let _end = End(self, stage);
        call()
    }

    #[inline]
    pub fn wake(&self, periodic_deadline: Option<Instant>) {
        self.record(
            b'I',
            if periodic_deadline.is_some() {
                "wake_timer"
            } else {
                "wake_readiness"
            },
        );
        if let Some(observe) = &self.observer {
            observe(Timing::Wake {
                at: Instant::now(),
                periodic_deadline,
            });
        }
    }

    #[inline]
    fn ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
        self.scope("bus_transfer", || self.observed_ioctl(call))
    }

    fn observed_ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
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
        self.record(
            b'I',
            if complete {
                "drain_complete"
            } else {
                "drain_incomplete"
            },
        );
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

    #[test]
    fn source_labels_preserve_stage_and_separate_sanitized_or_shortened_names() {
        let label = |source: Source, stage| {
            let (bytes, len) = source.label(stage);
            String::from_utf8(bytes[..len].to_vec()).unwrap()
        };
        assert_eq!(
            label(Source::new("imu", "primary"), "drain"),
            "imu/primary/drain"
        );
        let first = label(Source::new("imu", "very_long_sensor_name_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_A"), "bus_transfer");
        let second = label(Source::new("imu", "very_long_sensor_name_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_abcdefghijklmnopqrstuvwxyz_B"), "bus_transfer");
        assert_ne!(first, second);
        assert!(
            first.contains('~'),
            "long names retain an explicit fingerprint"
        );
        assert!(first.len() <= 96 && first.ends_with("/bus_transfer"));
        assert_ne!(
            label(Source::new("gnss", "a|b"), "read"),
            label(Source::new("gnss", "a_b"), "read")
        );
        assert!(!label(Source::new("gnss", "a\nb"), "read").contains(['\n', '|']));
        let trace = Trace::for_source("imu", "primary");
        let result: Result<(), u8> = trace.scope("drain", || Err(7));
        assert_eq!(result, Err(7));
        assert!(
            trace.observer.is_none(),
            "native scopes allocate no callback"
        );
    }

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

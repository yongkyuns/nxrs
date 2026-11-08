//! Ordinary acquisition carries no diagnostic state or observer checks.
use crate::{probe::Timing, DeviceError};
use std::time::Instant;

#[derive(Clone, Default)]
pub(crate) struct Trace {}

impl Trace {
    pub(crate) fn new(
        _: impl Fn(Timing) + Send + Sync + 'static,
        _: bool,
    ) -> Result<Self, DeviceError> {
        Err(DeviceError::Unsupported)
    }

    #[inline]
    pub(crate) fn sensor_reference(&self) -> bool {
        false
    }

    #[inline]
    pub(crate) fn wake(&self, _: Option<Instant>) {}

    #[inline]
    pub(crate) fn ioctl<T>(&self, call: impl FnOnce() -> T) -> T {
        call()
    }

    #[inline]
    pub(crate) fn drain(&self, _: bool) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_tracing_is_zero_sized_and_passes_io_through_once() {
        assert_eq!(std::mem::size_of::<Trace>(), 0);
        let trace = Trace::default();
        assert!(!trace.sensor_reference());
        trace.wake(None);
        trace.drain(true);
        let mut calls = 0;
        let result = trace.ioctl(|| {
            calls += 1;
            Err::<(), _>(DeviceError::Io)
        });
        assert_eq!(result, Err(DeviceError::Io));
        assert_eq!(calls, 1);
        assert_eq!(
            Trace::new(|_| panic!("disabled observer called"), true).err(),
            Some(DeviceError::Unsupported)
        );
    }
}

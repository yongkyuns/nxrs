//! Ordinary acquisition carries no diagnostic state or observer checks.
use crate::{probe::Timing, DeviceError};
use std::time::Instant;

#[derive(Clone, Default)]
pub(crate) struct Trace {}

pub(crate) type Observed<T> = T;

impl Trace {
    pub(crate) fn observe<T>(self, inner: T) -> T {
        inner
    }
    pub(crate) fn new(_: impl Fn(Timing) + Send + Sync + 'static) -> Result<Self, DeviceError> {
        Err(DeviceError::Unsupported)
    }

    #[inline]
    pub(crate) fn wake(&self, _: Option<Instant>) {}

    #[inline]
    pub(crate) fn drain(&self, _: bool) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_tracing_is_zero_sized_and_leaves_the_resource_unwrapped() {
        assert_eq!(std::mem::size_of::<Trace>(), 0);
        let trace = Trace::default();
        trace.wake(None);
        trace.drain(true);
        let resource = std::boxed::Box::new(7);
        let address = &*resource as *const i32;
        let direct: std::boxed::Box<i32> = trace.observe(resource);
        assert_eq!(&*direct as *const i32, address);
        assert_eq!(
            Trace::new(|_| panic!("disabled observer called")).err(),
            Some(DeviceError::Unsupported)
        );
    }
}

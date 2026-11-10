//! Ordinary acquisition carries no diagnostic state or observer checks.
use super::Timing;
use std::time::Instant;

#[derive(Clone, Default)]
pub struct Trace {}

pub type Observed<T> = T;

impl Trace {
    pub fn observe<T>(self, inner: T) -> T {
        inner
    }
    /// Returns `None` when instrumentation was not compiled with `probe`.
    pub fn new(_: impl Fn(Timing) + Send + Sync + 'static) -> Option<Self> {
        None
    }

    #[inline]
    pub fn wake(&self, _: Option<Instant>) {}

    #[inline]
    pub fn drain(&self, _: bool) {}
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
        assert!(Trace::new(|_| panic!("disabled observer called")).is_none());
    }
}

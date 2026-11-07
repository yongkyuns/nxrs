//! NuttX acquisition and process-global scan lease.
#![deny(unsafe_op_in_unsafe_fn)]

use crate::worker::Lease;
#[cfg(not(test))]
use crate::worker::Worker;
#[cfg(not(test))]
use crate::Error;
#[cfg(not(test))]
use crate::Event;

mod native;
#[cfg_attr(test, allow(dead_code))]
mod peers;
#[cfg(test)]
mod tests;

#[cfg(not(test))]
pub(crate) fn open<S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static>(
    sink: S,
) -> Result<Worker, Error> {
    Worker::start_with_stack(native::Native::new(), sink, Lease::acquire()?, 8192)
}

#[cfg(not(test))]
pub(crate) mod gatt;

#[cfg(hal_provider = "hardware")]
pub(crate) fn gatt_driver<const N: usize>(
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<impl crate::gatt::Driver<N>, crate::Error> {
    #[cfg(test)]
    type Backend = crate::gatt_driver::tests::FakeBackend;
    #[cfg(not(test))]
    type Backend = gatt::Executor;
    crate::gatt_driver::open_with::<N, Backend, crate::gatt_driver::NativePeer, _>(wake)
}

//! Platform Rust implementation and its private SDK glue.
use crate::shared::{Report, ScanBackend};
#[cfg(not(test))]
pub(crate) mod gatt;
use linux_gio as gio;
mod identity;
mod scan;

pub(super) use scan::State;

pub(crate) fn open<S>(sink: S) -> Result<crate::worker::Worker, crate::Error>
where
    S: FnMut(crate::Event) -> messaging::DeliveryResult<crate::Event> + Send + 'static,
{
    crate::shared::open::<State, S>(sink)
}

#[cfg(hal_enabled)]
pub(crate) fn gatt_driver<const N: usize>(
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<impl crate::gatt::Driver<N>, crate::Error> {
    #[cfg(test)]
    type Backend = crate::gatt_driver::tests::FakeBackend;
    #[cfg(not(test))]
    type Backend = gatt::Executor;
    crate::gatt_driver::open_with::<N, Backend, crate::shared::DesktopPeer, _>(wake)
}

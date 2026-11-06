//! Platform Rust implementation and its private SDK glue.
use crate::shared::Station;
use linux_gio as gio;
mod station;

pub(super) use station::State;

#[path = "../unbound.rs"]
mod unbound;
pub(crate) use unbound::open_quiescence;
pub(crate) fn open<S>(
    sink: S,
    storage: crate::CredentialStorage,
) -> Result<crate::worker::Worker, crate::Error>
where
    S: FnMut(crate::Event) -> messaging::DeliveryResult<crate::Event> + Send + 'static,
{
    crate::shared::open::<State, S>(sink, storage)
}

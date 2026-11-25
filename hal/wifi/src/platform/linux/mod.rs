//! Platform Rust implementation and its private SDK glue.
use crate::platform::shared::Station;
use linux_gio as gio;
mod station;

pub(super) use station::State;

pub(crate) use super::unsupported_quiescence::open_quiescence;
pub(crate) fn open<S>(
    sink: S,
    storage: crate::CredentialStorage,
) -> Result<crate::platform::worker::Worker, crate::Error>
where
    S: FnMut(crate::Event) -> messaging::DeliveryResult<crate::Event> + Send + 'static,
{
    crate::platform::shared::open::<State, S>(sink, storage)
}

//! Platform Rust implementation and its private SDK glue.
use crate::shared::Station;
mod station;

pub(super) use station::State;

use super::unbound;
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

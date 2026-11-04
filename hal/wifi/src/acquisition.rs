use crate::{Controller, CredentialStorage, Error, Event, QuiescenceControl, Wifi};

pub fn open<S>(sink: S) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
{
    open_with_storage(sink, CredentialStorage::Volatile)
}

/// Acquire Wi-Fi with an explicit credential persistence policy.
pub fn open_with_storage<S>(
    sink: S,
    storage: CredentialStorage,
) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: FnMut(Event) -> messaging::DeliveryResult<Event> + Send + 'static,
{
    crate::platform::open(sink, storage).map(Controller::new)
}

/// Acquire exclusive control after all Wi-Fi sessions have disconnected,
/// stopped and joined. Acquisition leaves the interface active; call
/// `quiesce` before flash operations, then `resume` before reopening Wi-Fi.
/// Unsupported providers fail without changing the radio. An uncertain
/// transition is sticky and quarantines admission until reset, even on Drop.
/// Dropping a quiet owner leaves the interface down; retain it to resume.
pub fn open_for_quiescence() -> Result<impl QuiescenceControl, Error> {
    crate::platform::open_quiescence()
}

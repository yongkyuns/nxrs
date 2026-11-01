//! OS selection is confined to this wiring module.
#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
#[path = "../quiescence.rs"]
pub(crate) mod quiescence;
pub mod acquisition {
    use crate::{Controller, CredentialStorage, Error, Event, EventSink, QuiescenceControl, Wifi};

    pub fn open<S>(sink: S) -> Result<Controller<impl Wifi + Send>, Error>
    where
        S: EventSink<Event> + Send + 'static,
    {
        open_with_storage(sink, CredentialStorage::Volatile)
    }

    /// Acquire Wi-Fi with an explicit credential persistence policy.
    pub fn open_with_storage<S>(
        sink: S,
        storage: CredentialStorage,
    ) -> Result<Controller<impl Wifi + Send>, Error>
    where
        S: EventSink<Event> + Send + 'static,
    {
        super::open(sink, storage).map(Controller::new)
    }

    /// Acquire exclusive control after all Wi-Fi sessions have disconnected,
    /// stopped and joined. Acquisition leaves the interface active; call
    /// `quiesce` before flash operations, then `resume` before reopening Wi-Fi.
    /// Unsupported providers fail without changing the radio. An uncertain
    /// transition is sticky and quarantines admission until reset, even on Drop.
    /// Dropping a quiet owner leaves the interface down; retain it to resume.
    pub fn open_for_quiescence() -> Result<impl QuiescenceControl, Error> {
        super::open_quiescence()
    }
}

#[cfg(all(hal_enabled, target_os = "nuttx"))]
fn open_quiescence() -> Result<impl crate::QuiescenceControl, crate::Error> {
    nuttx::open_for_quiescence()
}

#[cfg(all(hal_enabled, not(target_os = "nuttx")))]
fn open_quiescence() -> Result<impl crate::QuiescenceControl, crate::Error> {
    struct Unsupported;
    impl crate::QuiescenceControl for Unsupported {
        fn quiesce(&mut self) -> Result<(), crate::Error> {
            Err(crate::Error::Unsupported)
        }
        fn resume(&mut self) -> Result<(), crate::Error> {
            Err(crate::Error::Unsupported)
        }
    }
    Err::<Unsupported, _>(crate::Error::Unsupported)
}
#[cfg(all(hal_enabled, target_os = "linux"))]
#[allow(unsafe_code)]
pub(crate) mod linux;
#[cfg(all(hal_enabled, target_os = "macos"))]
#[allow(unsafe_code)]
pub(crate) mod macos;
#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
pub mod nuttx;
#[cfg(all(hal_enabled, not(target_os = "nuttx")))]
pub(crate) fn open<S>(
    sink: S,
    storage: crate::CredentialStorage,
) -> Result<crate::worker::Worker, crate::Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    #[cfg(target_os = "linux")]
    type State = linux::State;
    #[cfg(target_os = "macos")]
    type State = macos::State;
    crate::shared::open::<State, S>(sink, storage)
}
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub(crate) fn open<S>(
    sink: S,
    _: crate::CredentialStorage,
) -> Result<crate::worker::Worker, crate::Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    nuttx::open(sink)
}

#[cfg(not(hal_enabled))]
mod unbound;
#[cfg(not(hal_enabled))]
fn open<S>(_: S, _: crate::CredentialStorage) -> Result<impl crate::Wifi + Send, crate::Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    Err::<unbound::Unavailable, _>(crate::Error::Unsupported)
}
#[cfg(not(hal_enabled))]
fn open_quiescence() -> Result<impl crate::QuiescenceControl, crate::Error> {
    Err::<unbound::Unavailable, _>(crate::Error::Unsupported)
}

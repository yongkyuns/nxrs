//! OS selection is confined to this wiring module.
#[cfg(all(hal_enabled, target_os = "linux"))]
#[allow(unsafe_code)]
pub(crate) mod linux;
#[cfg(all(hal_enabled, target_os = "macos"))]
#[allow(unsafe_code)]
pub(crate) mod macos;
#[cfg(any(all(hal_enabled, target_os = "nuttx"), test))]
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

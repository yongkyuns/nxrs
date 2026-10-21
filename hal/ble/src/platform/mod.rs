//! Platform-selected wiring; implementations stay inside this capability.
pub(crate) mod gatt;
pub(crate) mod gatt_backend;
#[cfg(nxrs_hal_platform = "linux")]
#[allow(unsafe_code)]
pub(crate) mod linux;
#[cfg(nxrs_hal_platform = "macos")]
#[allow(unsafe_code)]
pub(crate) mod macos;
#[cfg(any(nxrs_hal_platform = "nuttx", test))]
pub mod nuttx;
#[cfg(any(nxrs_hal_platform = "linux", nxrs_hal_platform = "macos", test))]
#[path = "std/mod.rs"]
pub(crate) mod standard;

#[cfg(any(nxrs_hal_platform = "linux", nxrs_hal_platform = "macos"))]
pub(crate) fn open<S>(sink: S) -> Result<crate::worker::Worker, crate::Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    #[cfg(nxrs_hal_platform = "linux")]
    type State = linux::State;
    #[cfg(nxrs_hal_platform = "macos")]
    type State = macos::State;
    standard::open::<State, S>(sink)
}

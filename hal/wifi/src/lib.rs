//! Wi-Fi controller and contracts; platform configuration selects acquisition.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(test, nxrs_provider = "nuttx", nxrs_provider = "native"))]
extern crate std;

mod api;
pub use api::*;

mod controller;
pub use controller::{Controller, ControllerError, Outcome, Update};

#[cfg(any(nxrs_provider = "nuttx", nxrs_provider = "native", all(test, unix)))]
mod providers;

#[cfg(any(nxrs_provider = "nuttx", nxrs_provider = "native", all(test, unix)))]
mod worker;

#[cfg(all(
    nxrs_provider = "native",
    not(any(target_os = "macos", target_os = "linux"))
))]
compile_error!("Native Wi-Fi is implemented for macOS and Linux only");

/// Open with volatile credentials. macOS supports scan/link observation but
/// refuses joins under this default; see `open_with_storage` for explicit opt-in.
#[cfg(any(
    nxrs_provider = "nuttx",
    all(
        nxrs_provider = "native",
        any(target_os = "macos", target_os = "linux")
    )
))]
pub fn open<S>(sink: S) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    open_with_storage(sink, CredentialStorage::Volatile)
}

/// Open with an explicit credential-storage policy for this controller's lifetime.
/// `SystemManaged` permits saved credentials; it does not require persistence.
/// Inspect capabilities for the provider's native behavior. Scanning/observation
/// never requires this opt-in. Stop/disconnect do not delete OS-managed secrets.
///
/// macOS joins require `SystemManaged` and exclusive control of the interface:
/// CoreWLAN has no per-client association lease. Existing/replacement link checks
/// cannot prevent races with another process using the same interface.
#[cfg(any(
    nxrs_provider = "nuttx",
    all(
        nxrs_provider = "native",
        any(target_os = "macos", target_os = "linux")
    )
))]
pub fn open_with_storage<S>(
    sink: S,
    storage: CredentialStorage,
) -> Result<Controller<impl Wifi + Send>, Error>
where
    S: EventSink<Event> + Send + 'static,
{
    #[cfg(nxrs_provider = "nuttx")]
    {
        // Permitting OS storage never forces a provider to save credentials.
        let _ = storage;
        providers::nuttx::open(sink).map(Controller::new)
    }
    #[cfg(nxrs_provider = "native")]
    providers::native::open(sink, storage).map(Controller::new)
}

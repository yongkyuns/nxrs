//! GATT exports and platform-selected acquisition.
#![forbid(unsafe_code)]

mod client;
pub use client::*;

/// Open the platform-selected native client with an existing owner wakeup.
/// The wake function must be nonblocking, infallible and coalescing-safe (for
/// example, unpark the service owner). Drain poll() before the owner waits.
/// The wake context's destructor runs on the backend's callback executor
/// (LPWORK on NuttX) and must not block.
/// NuttX supports at most 64 inline bytes, serialized LPWORK and flat builds;
/// writes larger than the negotiated ATT payload are explicitly unsupported.
/// Linux requires exclusive application control of the target peripheral:
/// BlueZ connections/disconnections are device-wide, not per-client leases.
/// Existing-link checks cannot prevent races with another process using it.
#[cfg(any(
    nxrs_hal_platform = "nuttx",
    nxrs_hal_platform = "linux",
    nxrs_hal_platform = "macos"
))]
pub fn open<const N: usize>(
    wake: impl Fn() + Send + Sync + 'static,
    recovery_timeout_ms: u64,
) -> Result<Client<impl Driver<N>, N>, crate::Error> {
    if recovery_timeout_ms == 0 {
        return Err(crate::Error::InvalidValue);
    }
    #[cfg(test)]
    type Backend = crate::platform::gatt::tests::FakeBackend;
    #[cfg(all(not(test), nxrs_hal_platform = "nuttx"))]
    type Backend = crate::platform::nuttx::gatt::Executor;
    #[cfg(all(not(test), nxrs_hal_platform = "macos"))]
    type Backend = crate::platform::macos::gatt::Executor;
    #[cfg(all(not(test), nxrs_hal_platform = "linux"))]
    type Backend = crate::platform::linux::gatt::Executor;
    #[cfg(nxrs_hal_platform = "nuttx")]
    let driver =
        crate::platform::gatt::open_with::<N, Backend, crate::platform::gatt::NativePeer, _>(wake)?;
    #[cfg(any(nxrs_hal_platform = "linux", nxrs_hal_platform = "macos"))]
    let driver =
        crate::platform::gatt::open_with::<N, Backend, crate::platform::standard::DesktopPeer, _>(
            wake,
        )?;
    Client::new(driver, recovery_timeout_ms).map_err(|_| crate::Error::InvalidValue)
}

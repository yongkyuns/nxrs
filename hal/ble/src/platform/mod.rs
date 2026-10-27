// OS selection is confined to this wiring module.
#[cfg(all(hal_enabled, target_os = "linux"))]
#[allow(unsafe_code)]
pub(crate) mod linux;
#[cfg(all(hal_enabled, target_os = "macos"))]
#[allow(unsafe_code)]
pub(crate) mod macos;
#[cfg(any(all(hal_enabled, target_os = "nuttx"), test))]
pub mod nuttx;
#[cfg(all(hal_enabled, not(target_os = "nuttx")))]
pub(crate) fn open<S>(sink: S) -> Result<crate::worker::Worker, crate::Error>
where
    S: crate::EventSink<crate::Event> + Send + 'static,
{
    #[cfg(target_os = "linux")]
    type State = linux::State;
    #[cfg(target_os = "macos")]
    type State = macos::State;
    crate::shared::open::<State, S>(sink)
}
#[cfg(all(hal_enabled, target_os = "nuttx"))]
pub(crate) use nuttx::open;

#[cfg(hal_enabled)]
pub(crate) fn gatt_driver<const N: usize>(
    wake: impl Fn() + Send + Sync + 'static,
) -> Result<impl crate::gatt::Driver<N>, crate::Error> {
    use crate::gatt_driver as driver;
    #[cfg(test)]
    type Backend = driver::tests::FakeBackend;
    #[cfg(all(not(test), target_os = "nuttx"))]
    type Backend = nuttx::gatt::Executor;
    #[cfg(all(not(test), target_os = "macos"))]
    type Backend = macos::gatt::Executor;
    #[cfg(all(not(test), target_os = "linux"))]
    type Backend = linux::gatt::Executor;
    #[cfg(target_os = "nuttx")]
    type Resolver = driver::NativePeer;
    #[cfg(not(target_os = "nuttx"))]
    type Resolver = crate::shared::DesktopPeer;
    driver::open_with::<N, Backend, Resolver, _>(wake)
}

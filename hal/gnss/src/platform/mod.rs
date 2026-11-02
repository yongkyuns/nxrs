//! Capability admission and resource acquisition.
#[cfg(any(test, hal_mock))]
pub(super) mod mock;
#[cfg(all(hal_enabled, target_os = "nuttx"))]
#[path = "nuttx/mod.rs"]
mod selected;
#[cfg(hal_mock)]
use mock as selected;
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{DeviceError, Event, EventSink, Gnss};
    struct Unconfigured;
    impl Gnss for Unconfigured {
        fn request_stop(&mut self) -> Result<(), DeviceError> {
            Ok(())
        }
        fn join(&mut self) -> Result<(), DeviceError> {
            Ok(())
        }
        fn is_finished(&self) -> bool {
            true
        }
        fn dropped(&self) -> u32 {
            0
        }
    }
    pub fn open<S: EventSink<Event> + Send + 'static>(
        _: S,
    ) -> Result<impl Gnss + Send + 'static, DeviceError> {
        Err::<Unconfigured, _>(DeviceError::Unsupported)
    }
}
pub use selected::open;

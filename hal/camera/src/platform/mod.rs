//! Camera admission and OS acquisition. Replay is explicit shared Rust code.
#[cfg(any(hal_mock, test))]
#[cfg_attr(test, allow(dead_code))]
mod mock;
#[cfg(any(all(hal_enabled, target_os = "nuttx"), all(test, unix)))]
#[cfg_attr(test, allow(dead_code))]
mod nuttx;
#[cfg(hal_mock)]
mod selected {
    pub use super::mock::{CameraAction, MockCamera, FORMAT};
    pub fn open() -> Result<impl crate::Camera, crate::DeviceError> {
        Ok(MockCamera::new([]))
    }
}
#[cfg(hal_enabled)]
mod selected {
    pub use crate::replay::{camera, camera as replay, ReplayCamera};
    #[cfg(target_os = "nuttx")]
    pub fn open() -> Result<impl crate::Camera, crate::DeviceError> {
        super::nuttx::LiveCamera::acquire()
    }
    #[cfg(not(target_os = "nuttx"))]
    pub fn open() -> Result<ReplayCamera, crate::DeviceError> {
        Err(crate::DeviceError::Unsupported)
    }
}
#[cfg(not(any(hal_enabled, hal_mock)))]
mod selected {
    use crate::{Camera, Capture, DeviceError, Format};
    pub struct UnconfiguredCamera;

    impl Camera for UnconfiguredCamera {
        fn start(&mut self, _: Format) -> Result<Format, DeviceError> {
            Err(DeviceError::Unsupported)
        }
        fn poll_frame(&mut self, _: u64, _: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
            Err(DeviceError::Unsupported)
        }
        fn stop(&mut self) -> Result<(), DeviceError> {
            Ok(())
        }
    }

    pub fn open() -> Result<UnconfiguredCamera, DeviceError> {
        Err(DeviceError::Unsupported)
    }
}
pub use selected::*;

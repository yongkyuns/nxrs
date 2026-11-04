use crate::{Config, Detector, DeviceError};
pub fn open(_: Config) -> Result<impl Detector, DeviceError> {
    struct Unavailable;
    impl Detector for Unavailable {
        fn capabilities(&self) -> crate::Capabilities {
            crate::Capabilities::presence(0, 0)
        }
        fn detect(&mut self, _: crate::Image<'_>) -> Result<crate::DetectionResult, DeviceError> {
            Err(DeviceError::Unsupported)
        }
    }
    Err::<Unavailable, _>(DeviceError::Unsupported)
}

#[cfg(all(feature = "probe", feature = "pico-model"))]
pub(crate) fn kernel() -> Result<crate::model::pico::Engine, crate::DeviceError> {
    Err(crate::DeviceError::Unsupported)
}

mod image;
#[cfg(feature = "person-model")]
pub(crate) mod person;
#[cfg(feature = "pico-model")]
pub(crate) mod pico;
#[cfg(feature = "qualification")]
pub(crate) use pico::kernel;
#[cfg(feature = "pico-model")]
pub(crate) use pico::open;

#[cfg(not(feature = "pico-model"))]
pub(crate) fn open(
    config: crate::Config,
) -> core::result::Result<impl crate::Detector, crate::DeviceError> {
    if config.require_localization {
        return Err(crate::DeviceError::Unsupported);
    }
    Ok(person::MobileNet::default())
}

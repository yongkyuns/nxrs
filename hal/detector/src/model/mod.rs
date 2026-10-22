mod image;
#[cfg(feature = "person-model")]
pub(crate) mod person;
#[cfg(detector_pico)]
pub(crate) mod pico;
#[cfg(detector_pico)]
impl pico::Kernel for crate::compute::esp_dl::Engine {
    fn run(
        &mut self,
        input: &[i8; pico::INPUT_BYTES],
        output: &mut [i8; pico::OUTPUT_BYTES],
    ) -> core::result::Result<(), crate::DeviceError> {
        self.run(input, output);
        Ok(())
    }
}
#[cfg(detector_pico)]
pub(crate) fn with_kernel<K: pico::Kernel>(
    config: crate::Config,
    kernel: K,
) -> core::result::Result<impl crate::Detector, crate::DeviceError> {
    let mut model = pico::Pico::new(kernel)?;
    model.set_box_threshold(config.box_threshold);
    Ok(model)
}
#[cfg(all(not(detector_pico), feature = "person-model"))]
pub(crate) fn open(
    config: crate::Config,
) -> core::result::Result<impl crate::Detector, crate::DeviceError> {
    if config.require_localization {
        return Err(crate::DeviceError::Unsupported);
    }
    Ok(person::MobileNet::default())
}

#[cfg(detector_pico)]
pub(crate) fn kernel() -> Result<crate::compute::esp_dl::Engine, crate::DeviceError> {
    crate::compute::esp_dl::Engine::new().map_err(|_| crate::DeviceError::Model)
}
#[cfg(detector_pico)]
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    with_kernel(config, kernel()?)
}

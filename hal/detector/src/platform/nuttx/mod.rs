//! NuttX owns its compute-context lease and reuses the Rust model behavior.
#[cfg(detector_pico)]
#[allow(unsafe_code)]
mod ffi;

#[cfg(detector_pico)]
struct Kernel(crate::compute::esp_dl::Engine);
#[cfg(detector_pico)]
impl crate::model::pico::Kernel for Kernel {
    fn run(
        &mut self,
        input: &[i8; crate::model::pico::INPUT_BYTES],
        output: &mut [i8; crate::model::pico::OUTPUT_BYTES],
    ) -> Result<(), crate::DeviceError> {
        ffi::run(|| self.0.run(input, output))
    }
}
#[cfg(detector_pico)]
pub(crate) fn kernel() -> Result<impl crate::model::pico::Kernel, crate::DeviceError> {
    let engine = crate::model::kernel()?;
    Ok(Kernel(engine))
}
#[cfg(detector_pico)]
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    crate::model::with_kernel(config, kernel()?)
}
#[cfg(not(detector_pico))]
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    crate::model::open(config)
}

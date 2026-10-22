//! NuttX owns its compute-context lease and reuses the Rust model behavior.
#[cfg(nxrs_detector_pico)]
#[allow(unsafe_code)]
mod ffi;

#[cfg(nxrs_detector_pico)]
struct Kernel(super::standard::ffi::Engine);
#[cfg(nxrs_detector_pico)]
impl super::standard::pico::Kernel for Kernel {
    fn run(
        &mut self,
        input: &[i8; super::standard::pico::INPUT_BYTES],
        output: &mut [i8; super::standard::pico::OUTPUT_BYTES],
    ) -> Result<(), crate::DeviceError> {
        ffi::run(|| self.0.run(input, output))
    }
}
#[cfg(nxrs_detector_pico)]
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    let engine = super::standard::ffi::Engine::new().map_err(|_| crate::DeviceError::Model)?;
    super::standard::with_kernel(config, Kernel(engine))
}
#[cfg(not(nxrs_detector_pico))]
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    super::standard::open(config)
}

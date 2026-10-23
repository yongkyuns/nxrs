//! NuttX owns its compute-context lease and reuses the Rust model behavior.
#[allow(unsafe_code)]
#[path = "ffi/mod.rs"]
mod ffi;

struct Kernel(crate::model::pico::Engine);
impl crate::model::pico::Kernel for Kernel {
    fn run(
        &mut self,
        input: &[i8; crate::model::pico::INPUT_BYTES],
        output: &mut [i8; crate::model::pico::OUTPUT_BYTES],
    ) -> Result<(), crate::DeviceError> {
        ffi::run(|| self.0.run(input, output))
    }
}
pub(crate) fn kernel() -> Result<impl crate::model::pico::Kernel, crate::DeviceError> {
    let engine = crate::model::pico::kernel()?;
    Ok(Kernel(engine))
}
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    crate::model::pico::with_kernel(config, kernel()?)
}

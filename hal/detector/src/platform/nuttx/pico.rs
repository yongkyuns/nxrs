//! NuttX owns its compute-context lease and reuses the Rust model behavior.
#[allow(unsafe_code)]
#[path = "ffi/mod.rs"]
mod ffi;

struct Kernel {
    engine: crate::model::pico::Engine,
    core: Option<u32>,
    failed: bool,
}
impl crate::model::pico::Kernel for Kernel {
    fn run(
        &mut self,
        input: &[i8; crate::model::pico::INPUT_BYTES],
        output: &mut [i8; crate::model::pico::OUTPUT_BYTES],
    ) -> Result<(), crate::DeviceError> {
        if self.failed {
            return Err(crate::DeviceError::Model);
        }
        let result = ffi::run(self.core, || self.engine.run(input, output));
        self.failed = result.is_err();
        result
    }
}
pub(crate) fn kernel() -> Result<impl crate::model::pico::Kernel, crate::DeviceError> {
    let core = option_env!("DETECTOR_COMPUTE_CORE")
        .map(str::parse::<u32>)
        .transpose()
        .map_err(|_| crate::DeviceError::Model)?;
    if ffi::cpus() > 1 && core.is_none() {
        return Err(crate::DeviceError::Unsupported);
    }
    if core.is_some_and(|core| core >= ffi::cpus()) {
        return Err(crate::DeviceError::Unsupported);
    }
    let engine = crate::model::pico::kernel()?;
    Ok(Kernel {
        engine,
        core,
        failed: false,
    })
}
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    crate::model::pico::with_kernel(config, kernel()?)
}

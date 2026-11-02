//! NuttX owns its compute-context lease and reuses the Rust model behavior.
#[allow(unsafe_code)]
#[path = "ffi/mod.rs"]
mod ffi;

use crate::model::pico::compute::{Engine, Error as ComputeError, Partition, Worker};
struct Kernel {
    engine: Engine,
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
        let result = ffi::run(self.core, || {
            self.engine
                .run(input, output)
                .map_err(|_| crate::DeviceError::Model)
        });
        self.failed = result.is_err();
        result
    }
}
pub(crate) fn kernel() -> Result<impl crate::model::pico::Kernel, crate::DeviceError> {
    let core = option_env!("DETECTOR_CALLER_CORE")
        .map(str::parse::<u32>)
        .transpose()
        .map_err(|_| crate::DeviceError::Model)?;
    if ffi::cpus() > 1 && core.is_none() {
        return Err(crate::DeviceError::Unsupported);
    }
    let engine = if let Some(core) = core {
        let helper = option_env!("DETECTOR_HELPER_CORE")
            .and_then(|v| v.parse::<u32>().ok())
            .ok_or(crate::DeviceError::Model)?;
        let stack = option_env!("DETECTOR_HELPER_STACK")
            .and_then(|v| v.parse::<usize>().ok())
            .ok_or(crate::DeviceError::Model)?;
        let priority = option_env!("DETECTOR_HELPER_PRIORITY")
            .and_then(|v| v.parse::<i32>().ok())
            .ok_or(crate::DeviceError::Model)?;
        if core >= ffi::cpus() || helper >= ffi::cpus() || core == helper {
            return Err(crate::DeviceError::Unsupported);
        }
        Engine::with_parallel(
            stack,
            move || ffi::initialize_helper(helper, priority).map_err(|_| ComputeError::Model),
            |work| ffi::numerical(|| work.run()),
            |worker: &mut Worker<Partition>, first, second| {
                ffi::paused(|| worker.run(first, second))
            },
        )
        .map_err(|_| crate::DeviceError::Model)?
    } else {
        crate::model::pico::kernel()?
    };
    Ok(Kernel {
        engine,
        core,
        failed: false,
    })
}
pub(crate) fn open(config: crate::Config) -> Result<impl crate::Detector, crate::DeviceError> {
    crate::model::pico::with_kernel(config, kernel()?)
}

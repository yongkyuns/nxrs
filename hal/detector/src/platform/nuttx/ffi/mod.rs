//! Single-core native compute lease. Capture never acquires this mutex.
use crate::DeviceError;
use std::sync::Mutex;

static COMPUTE: Mutex<()> = Mutex::new(());
unsafe extern "C" {
    fn detector_compute_enabled() -> i32;
    fn detector_compute_set(enabled: i32);
}
struct Context {
    previous: bool,
}
impl Context {
    fn enter() -> Self {
        // SAFETY: target-header access on the current qualified CPU. COMPUTE
        // serializes detector kernels; no SDK callbacks or tasks outlive run.
        let previous = unsafe { detector_compute_enabled() } != 0;
        if !previous {
            unsafe { detector_compute_set(1) };
        }
        Self { previous }
    }
}
impl Drop for Context {
    fn drop(&mut self) {
        if !self.previous {
            // SAFETY: synchronous SDK work has finished before this guard drops.
            // Keep a context enabled by an earlier owner enabled.
            unsafe { detector_compute_set(0) };
        }
    }
}
pub(super) fn run(operation: impl FnOnce()) -> Result<(), DeviceError> {
    // A poisoned lease is a sticky failure, not permission to reuse uncertain
    // native state. Multiple detector owners serialize only numerical execution.
    let _exclusive = COMPUTE.lock().map_err(|_| DeviceError::Model)?;
    let _context = Context::enter();
    operation();
    // Context restores before the exclusive lease is released, including unwind.
    Ok(())
}

//! Scoped native compute/placement leases. Capture never acquires COMPUTE.
use crate::DeviceError;
use std::sync::Mutex;

static COMPUTE: Mutex<()> = Mutex::new(());
unsafe extern "C" {
    fn detector_compute_enabled() -> i32;
    fn detector_compute_set(enabled: i32);
    fn detector_compute_cpus() -> i32;
    fn detector_affinity_get(mask: *mut u32) -> i32;
    fn detector_affinity_set(mask: u32) -> i32;
    fn detector_current_cpu() -> i32;
    fn detector_helper_priority(priority: i32) -> i32;
}
pub(super) fn cpus() -> u32 {
    unsafe { detector_compute_cpus() }.max(0) as u32
}
fn affinity() -> Result<u32, DeviceError> {
    let mut mask = 0;
    if unsafe { detector_affinity_get(&mut mask) } == 0 {
        Ok(mask)
    } else {
        Err(DeviceError::Model)
    }
}
fn pin(core: u32) -> Result<(), DeviceError> {
    if core >= cpus() || core >= 32 {
        return Err(DeviceError::Unsupported);
    }
    if unsafe { detector_affinity_set(1 << core) } != 0
        || affinity()? != 1 << core
        || unsafe { detector_current_cpu() } != core as i32
    {
        return Err(DeviceError::Model);
    }
    Ok(())
}
pub(super) fn initialize_helper(core: u32, priority: i32) -> Result<(), DeviceError> {
    pin(core)?;
    if unsafe { detector_helper_priority(priority) } == 0 {
        Ok(())
    } else {
        Err(DeviceError::Model)
    }
}
struct Placement {
    prior: u32,
    restored: bool,
}
impl Placement {
    fn enter(core: u32) -> Result<Self, DeviceError> {
        let mut guard = Self {
            prior: affinity()?,
            restored: false,
        };
        if let Err(error) = pin(core) {
            let _ = guard.restore();
            return Err(error);
        }
        Ok(guard)
    }
    fn restore(&mut self) -> Result<(), DeviceError> {
        if unsafe { detector_affinity_set(self.prior) } != 0 || affinity()? != self.prior {
            return Err(DeviceError::Model);
        }
        self.restored = true;
        Ok(())
    }
}
impl Drop for Placement {
    fn drop(&mut self) {
        if !self.restored {
            let _ = self.restore();
        }
    }
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
pub(super) fn numerical<T>(operation: impl FnOnce() -> T) -> T {
    let _context = Context::enter();
    operation()
}
/// The SDK callback has finished its caller-side SIMD work before waiting.
pub(super) fn paused<T>(operation: impl FnOnce() -> T) -> T {
    struct Resume(bool);
    impl Drop for Resume {
        fn drop(&mut self) {
            if self.0 {
                unsafe { detector_compute_set(1) };
            }
        }
    }
    let prior = unsafe { detector_compute_enabled() } != 0;
    if prior {
        unsafe { detector_compute_set(0) };
    }
    let _resume = Resume(prior);
    operation()
}
pub(super) fn run(
    core: Option<u32>,
    operation: impl FnOnce() -> Result<(), DeviceError>,
) -> Result<(), DeviceError> {
    // A poisoned lease is a sticky failure, not permission to reuse uncertain
    // native state. Multiple detector owners serialize only numerical execution.
    let _exclusive = COMPUTE.lock().map_err(|_| DeviceError::Model)?;
    let mut placement = core.map(Placement::enter).transpose()?;
    let result = numerical(operation);
    // Restore CP3 before allowing migration or another owner, including unwind.
    let restored = placement.as_mut().map_or(Ok(()), Placement::restore);
    result.and(restored)
}

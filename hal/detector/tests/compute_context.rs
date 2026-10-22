//! Host-injected SDK witness for the owning Rust lease, not a production C API.
use detector::DeviceError;
use std::sync::atomic::{AtomicI32, Ordering};
#[path = "../src/platform/nuttx/ffi/mod.rs"]
mod context;
static ENABLED: AtomicI32 = AtomicI32::new(0);
#[unsafe(no_mangle)]
extern "C" fn nxrs_detector_compute_enabled() -> i32 {
    ENABLED.load(Ordering::SeqCst)
}
#[unsafe(no_mangle)]
extern "C" fn nxrs_detector_compute_set(enabled: i32) {
    ENABLED.store(enabled, Ordering::SeqCst);
}
#[test]
fn scoped_compute_restores_prior_state_and_retires_on_unwind() {
    context::run(|| assert_eq!(ENABLED.load(Ordering::SeqCst), 1)).unwrap();
    assert_eq!(ENABLED.load(Ordering::SeqCst), 0);
    ENABLED.store(1, Ordering::SeqCst);
    context::run(|| assert_eq!(ENABLED.load(Ordering::SeqCst), 1)).unwrap();
    assert_eq!(ENABLED.load(Ordering::SeqCst), 1);
    ENABLED.store(0, Ordering::SeqCst);
    assert!(std::panic::catch_unwind(|| context::run(|| panic!("SDK witness unwind"))).is_err());
    assert_eq!(ENABLED.load(Ordering::SeqCst), 0);
    assert_eq!(
        context::run(|| panic!("poisoned lease must reject admission")),
        Err(DeviceError::Model)
    );
}

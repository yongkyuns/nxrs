//! Host state-machine witness; target header/CPU behavior needs device checks.
use detector::DeviceError;
use std::cell::Cell;
#[path = "../src/platform/nuttx/ffi/mod.rs"]
mod context;
thread_local! {
    static MASK: Cell<u32> = const { Cell::new(3) };
    static ENABLED: Cell<bool> = const { Cell::new(false) };
    static REFUSE: Cell<bool> = const { Cell::new(false) };
    static STUCK: Cell<bool> = const { Cell::new(false) };
    static ON_RESTORE: Cell<bool> = const { Cell::new(false) };
}
#[no_mangle]
extern "C" fn detector_compute_cpus() -> i32 {
    2
}
#[no_mangle]
extern "C" fn detector_compute_enabled() -> i32 {
    ENABLED.get() as i32
}
#[no_mangle]
extern "C" fn detector_compute_set(value: i32) {
    ENABLED.set(value != 0);
}
#[no_mangle]
unsafe extern "C" fn detector_affinity_get(result: *mut u32) -> i32 {
    unsafe {
        *result = MASK.get();
    }
    0
}
#[no_mangle]
extern "C" fn detector_affinity_set(value: u32) -> i32 {
    if value == 2 && REFUSE.get() {
        return -1;
    }
    if value == 3 {
        ON_RESTORE.set(ENABLED.get());
    }
    MASK.set(value);
    0
}
#[no_mangle]
extern "C" fn detector_current_cpu() -> i32 {
    if STUCK.get() {
        0
    } else {
        MASK.get().trailing_zeros() as i32
    }
}
#[test]
fn placement_context_failure_and_unwind_restore_in_order() {
    assert_eq!(context::cpus(), 2);
    assert_eq!(
        context::run(Some(2), || panic!("invalid core")),
        Err(DeviceError::Unsupported)
    );
    assert_eq!(
        context::run(Some(1), || {
            assert_eq!(MASK.get(), 2);
            assert!(ENABLED.get());
        }),
        Ok(())
    );
    assert_eq!(MASK.get(), 3);
    assert!(!ENABLED.get());
    STUCK.set(true);
    assert_eq!(
        context::run(Some(1), || panic!("not placed")),
        Err(DeviceError::Model)
    );
    assert_eq!(MASK.get(), 3);
    STUCK.set(false);
    assert!(!ON_RESTORE.get());
    ENABLED.set(true);
    context::run(Some(1), || {
        assert!(ENABLED.get());
    })
    .unwrap();
    assert!(ENABLED.get());
    assert!(ON_RESTORE.get());
    ENABLED.set(false);
    REFUSE.set(true);
    assert_eq!(
        context::run(Some(1), || panic!("no admission")),
        Err(DeviceError::Model)
    );
    assert_eq!(MASK.get(), 3);
    assert!(!ENABLED.get());
    REFUSE.set(false);
    assert!(std::panic::catch_unwind(|| context::run(Some(1), || panic!("numerical"))).is_err());
    assert_eq!(MASK.get(), 3);
    assert!(!ENABLED.get());
    assert_eq!(
        context::run(Some(1), || panic!("poisoned")),
        Err(DeviceError::Model)
    );
}

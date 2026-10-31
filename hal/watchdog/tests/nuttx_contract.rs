//! Exercise the real Rust owner with SDK-call fakes; no host watchdog is opened.
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU8, AtomicUsize, Ordering};
use watchdog::{ArmedWatchdog, DeviceError};

#[path = "../src/platform/nuttx/mod.rs"]
#[allow(dead_code)]
mod provider;

static FLAGS: AtomicU8 = AtomicU8::new(3);
static QUERY_FAIL: AtomicBool = AtomicBool::new(false);
static STOP_RESULT: AtomicI32 = AtomicI32::new(0);
static IGNORE_STOP: AtomicBool = AtomicBool::new(false);
static STOPS: AtomicUsize = AtomicUsize::new(0);

#[unsafe(no_mangle)]
unsafe extern "C" fn watchdog_reset_status(fd: i32, active: *mut bool, reset: *mut bool) -> i32 {
    assert!(fd >= 0);
    if QUERY_FAIL.load(Ordering::SeqCst) {
        return -1;
    }
    let flags = FLAGS.load(Ordering::SeqCst);
    unsafe {
        *active = flags & 1 != 0;
        *reset = flags & 2 != 0;
    }
    0
}

#[unsafe(no_mangle)]
extern "C" fn watchdog_disarm(fd: i32) -> i32 {
    assert!(fd >= 0);
    STOPS.fetch_add(1, Ordering::SeqCst);
    let result = STOP_RESULT.load(Ordering::SeqCst);
    if result == 0 && !IGNORE_STOP.load(Ordering::SeqCst) {
        FLAGS.fetch_and(!1, Ordering::SeqCst);
    }
    result
}

#[test]
fn inherited_deadline_is_exclusive_never_restarted_and_only_explicitly_cancelled() {
    let temporary = std::env::temp_dir().join(format!("nxrs-watchdog-{}.test", std::process::id()));
    std::fs::write(&temporary, []).unwrap();
    let device = temporary.as_path();
    assert!(matches!(watchdog::adopt(), Err(DeviceError::Unsupported)));
    assert!(matches!(
        provider::adopt_at(&temporary.with_extension("missing")),
        Err(DeviceError::Io)
    ));
    for flags in [0, 1, 2] {
        FLAGS.store(flags, Ordering::SeqCst);
        assert!(matches!(
            provider::adopt_at(device),
            Err(DeviceError::InvalidState)
        ));
    }
    FLAGS.store(3, Ordering::SeqCst);
    QUERY_FAIL.store(true, Ordering::SeqCst);
    assert!(matches!(provider::adopt_at(device), Err(DeviceError::Io)));
    QUERY_FAIL.store(false, Ordering::SeqCst);

    let owner = provider::adopt_at(device).unwrap();
    assert!(matches!(provider::adopt_at(device), Err(DeviceError::Busy)));
    drop(owner);
    assert_eq!(FLAGS.load(Ordering::SeqCst), 3);
    assert_eq!(STOPS.load(Ordering::SeqCst), 0);
    assert!(std::panic::catch_unwind(|| {
        let _owner = provider::adopt_at(device).unwrap();
        panic!("startup failed");
    })
    .is_err());
    assert_eq!(STOPS.load(Ordering::SeqCst), 0);

    for (return_code, ignored, expected) in [
        (-1, false, DeviceError::Io),
        (0, true, DeviceError::InvalidState),
    ] {
        STOP_RESULT.store(return_code, Ordering::SeqCst);
        IGNORE_STOP.store(ignored, Ordering::SeqCst);
        let mut owner = provider::adopt_at(device).unwrap();
        assert_eq!(owner.disarm(), Err(expected));
        let count = STOPS.load(Ordering::SeqCst);
        assert_eq!(owner.disarm(), Err(expected));
        assert_eq!(STOPS.load(Ordering::SeqCst), count);
    }
    STOP_RESULT.store(0, Ordering::SeqCst);
    IGNORE_STOP.store(false, Ordering::SeqCst);
    let mut owner = provider::adopt_at(device).unwrap();
    owner.disarm().unwrap();
    let count = STOPS.load(Ordering::SeqCst);
    owner.disarm().unwrap();
    assert_eq!(STOPS.load(Ordering::SeqCst), count);
    assert_eq!(FLAGS.load(Ordering::SeqCst), 2);
    drop(owner);
    std::fs::remove_file(temporary).unwrap();
}

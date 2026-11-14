//! Exercise the real Rust owner with SDK-call fakes; no host watchdog is opened.
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU32, AtomicU8, AtomicUsize, Ordering};
use std::time::Duration;
use watchdog::{ArmedWatchdog, DeviceError, RuntimeWatchdog};

#[path = "../src/platform/nuttx/mod.rs"]
#[allow(dead_code)]
mod provider;

static FLAGS: AtomicU8 = AtomicU8::new(3);
static QUERY_FAIL: AtomicBool = AtomicBool::new(false);
static STOP_RESULT: AtomicI32 = AtomicI32::new(0);
static IGNORE_STOP: AtomicBool = AtomicBool::new(false);
static STOPS: AtomicUsize = AtomicUsize::new(0);
static TIMEOUT: AtomicU32 = AtomicU32::new(0);
static STARTS: AtomicUsize = AtomicUsize::new(0);
static FEEDS: AtomicUsize = AtomicUsize::new(0);

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

#[unsafe(no_mangle)]
extern "C" fn watchdog_set_timeout(fd: i32, ms: u32) -> i32 {
    assert!(fd >= 0);
    if ms > 10_000 {
        return 1;
    }
    TIMEOUT.store(ms, Ordering::SeqCst);
    0
}

#[unsafe(no_mangle)]
extern "C" fn watchdog_start(fd: i32) -> i32 {
    assert!(fd >= 0);
    assert_ne!(TIMEOUT.load(Ordering::SeqCst), 0);
    STARTS.fetch_add(1, Ordering::SeqCst);
    FLAGS.fetch_or(1, Ordering::SeqCst);
    0
}

#[unsafe(no_mangle)]
extern "C" fn watchdog_feed(fd: i32) -> i32 {
    assert!(fd >= 0);
    assert_eq!(FLAGS.load(Ordering::SeqCst), 3);
    FEEDS.fetch_add(1, Ordering::SeqCst);
    0
}

#[test]
fn reset_deadline_is_exclusive_and_only_explicitly_cancelled() {
    let temporary = std::env::temp_dir().join(format!("nxrs-watchdog-{}.test", std::process::id()));
    std::fs::write(&temporary, []).unwrap();
    let device = temporary.as_path();
    assert!(matches!(watchdog::adopt(), Err(DeviceError::Unsupported)));
    assert!(matches!(
        watchdog::arm(Duration::from_secs(1)),
        Err(DeviceError::Unsupported)
    ));
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

    assert_eq!(TIMEOUT.load(Ordering::SeqCst), 0);
    assert_eq!(STARTS.load(Ordering::SeqCst), 0);
    assert_eq!(FEEDS.load(Ordering::SeqCst), 0);
    for invalid in [
        Duration::ZERO,
        Duration::from_micros(1500),
        Duration::from_secs(11),
    ] {
        assert!(matches!(
            provider::arm_at(device, invalid),
            Err(DeviceError::InvalidTimeout)
        ));
    }
    assert_eq!(STARTS.load(Ordering::SeqCst), 0);
    let timeout = Duration::from_millis(1250);
    let mut owner = provider::arm_at(device, timeout).unwrap();
    assert!(matches!(provider::adopt_at(device), Err(DeviceError::Busy)));
    assert_eq!(TIMEOUT.load(Ordering::SeqCst), 1250);
    owner.feed().unwrap();
    assert_eq!(FEEDS.load(Ordering::SeqCst), 1);
    drop(owner);
    assert!(matches!(
        provider::arm_at(device, timeout),
        Err(DeviceError::InvalidState)
    ));
    assert_eq!(STARTS.load(Ordering::SeqCst), 1);
    provider::adopt_at(device).unwrap().disarm().unwrap();
    let mut owner = provider::arm_at(device, timeout).unwrap();
    owner.disarm().unwrap();
    assert_eq!(owner.feed(), Err(DeviceError::InvalidState));
    assert_eq!(FEEDS.load(Ordering::SeqCst), 1);
    std::fs::remove_file(temporary).unwrap();
}

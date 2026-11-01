//! Private ABI injection; physical persistence is qualified separately.
use super::*;
use std::sync::atomic::{AtomicI32, AtomicUsize, Ordering::SeqCst};

static STATUS: AtomicI32 = AtomicI32::new(0);
static LENGTH: AtomicUsize = AtomicUsize::new(0);
static CALLS: AtomicUsize = AtomicUsize::new(0);

#[no_mangle]
unsafe extern "C" fn config_get(_: i32, _: *const u8, _: *mut u8, length: *mut usize) -> i32 {
    CALLS.fetch_add(1, SeqCst);
    // SAFETY: the provider supplies its live length scalar.
    unsafe {
        *length = LENGTH.load(SeqCst);
    }
    STATUS.load(SeqCst)
}
#[no_mangle]
extern "C" fn config_set(_: i32, _: *const u8, _: *const u8, _: usize) -> i32 {
    CALLS.fetch_add(1, SeqCst);
    STATUS.load(SeqCst)
}
#[no_mangle]
extern "C" fn config_remove(_: i32, _: *const u8) -> i32 {
    CALLS.fetch_add(1, SeqCst);
    STATUS.load(SeqCst)
}

#[test]
fn native_read_bounds_missing_io_and_write_failures() {
    let mut store = Nvs(File::open("/dev/null").unwrap());
    for invalid in [
        "",
        "../key",
        "contains\0nul",
        "é",
        "abcdefghijklmnopqrstuvwxyz012345",
    ] {
        assert_eq!(store.get(invalid, &mut [0; 4]), Err(Error::InvalidInput));
        assert_eq!(store.set(invalid, b"x"), Err(Error::InvalidInput));
        assert_eq!(store.remove(invalid), Err(Error::InvalidInput));
    }
    assert_eq!(store.set("camera.wifi", b""), Err(Error::InvalidInput));
    assert_eq!(CALLS.load(SeqCst), 0);
    for (status, length, expected) in [
        (0, 4, Ok(Some(4))),
        (0, 5, Err(Error::BufferTooSmall)),
        (1, 0, Ok(None)),
        (-2, 0, Err(Error::Io)),
    ] {
        STATUS.store(status, SeqCst);
        LENGTH.store(length, SeqCst);
        assert_eq!(store.get("camera.wifi", &mut [0; 4]), expected);
    }
    for (status, expected) in [(0, Ok(())), (-1, Err(Error::Full)), (-2, Err(Error::Io))] {
        STATUS.store(status, SeqCst);
        assert_eq!(store.set("camera.wifi", b"value"), expected);
        assert_eq!(store.remove("camera.wifi"), expected);
    }
}

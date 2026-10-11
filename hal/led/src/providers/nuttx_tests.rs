//! Host tests of the real Rust provider; these symbols replace only its two
//! private C controls. This is not target ABI or hardware qualification.
use super::*;
use std::cell::RefCell;
use std::thread_local;

#[derive(Default)]
struct State {
    mask: u32,
    query_error: c_int,
    set_error: c_int,
    queries: usize,
    sets: usize,
    fd: c_int,
    index: u8,
    on: c_int,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

#[no_mangle]
unsafe extern "C" fn nxrs_userled_supported(fd: c_int, out: *mut u32) -> c_int {
    STATE.with(|cell| {
        let mut s = cell.borrow_mut();
        s.queries += 1;
        s.fd = fd;
        if s.query_error == 0 {
            // SAFETY: production caller provides an aligned live u32.
            unsafe {
                *out = s.mask;
            }
        }
        s.query_error
    })
}

#[no_mangle]
unsafe extern "C" fn nxrs_userled_set(fd: c_int, index: u8, on: c_int) -> c_int {
    STATE.with(|cell| {
        let mut s = cell.borrow_mut();
        s.sets += 1;
        s.fd = fd;
        s.index = index;
        s.on = on;
        s.set_error
    })
}

// All descriptor lifetime observations run in one test, without concurrent
// fixture opens. /proc assertions are Linux-host-only, never target evidence.
#[test]
fn provider_validation_errors_and_file_lifetime() {
    STATE.with(|s| {
        *s.borrow_mut() = State {
            mask: 0x8000_0005,
            ..State::default()
        }
    });
    let mut leds = UserLeds::open_path("/dev/null").unwrap();
    let fd = STATE.with(|s| s.borrow().fd);
    assert!(fd >= 0);
    assert_eq!(leds.supported().bits(), 0x8000_0005);
    leds.set(31, true).unwrap();
    STATE.with(|s| {
        let s = s.borrow();
        assert_eq!((s.fd, s.index, s.on, s.queries, s.sets), (fd, 31, 1, 1, 1));
    });
    leds.set(2, false).unwrap();
    assert_eq!(leds.set(1, true), Err(DeviceError::Unsupported));
    assert_eq!(leds.set(255, true), Err(DeviceError::InvalidData));
    STATE.with(|s| {
        let s = s.borrow();
        assert_eq!((s.index, s.on, s.sets), (2, 0, 2));
    });
    // Negative statuses are not valid errno; reject without pretending success.
    STATE.with(|s| s.borrow_mut().set_error = -1);
    assert_eq!(leds.set(0, true), Err(DeviceError::Io));
    drop(leds);
    assert_closed(fd);

    STATE.with(|s| s.borrow_mut().query_error = -1);
    assert!(matches!(
        UserLeds::open_path("/dev/null"),
        Err(DeviceError::Io)
    ));
    assert_closed(STATE.with(|s| s.borrow().fd));
    let queries = STATE.with(|s| s.borrow().queries);
    // Interior NUL must be rejected by std, before reaching either helper.
    assert!(UserLeds::open_path("/dev/invalid\0node").is_err());
    assert_eq!(STATE.with(|s| s.borrow().queries), queries);

    for (kind, expected) in [
        (io::ErrorKind::WouldBlock, DeviceError::Busy),
        (io::ErrorKind::Interrupted, DeviceError::Busy),
        (io::ErrorKind::TimedOut, DeviceError::Timeout),
        (io::ErrorKind::Unsupported, DeviceError::Unsupported),
        (io::ErrorKind::InvalidInput, DeviceError::InvalidData),
        (io::ErrorKind::Other, DeviceError::Io),
    ] {
        assert_eq!(device_error(io::Error::from(kind)), expected);
    }
    assert_eq!(status(0), Ok(()));
    assert!(status(12345).is_err()); // No particular host errno value assumed.
}

fn assert_closed(fd: c_int) {
    #[cfg(target_os = "linux")]
    assert!(std::fs::read_link(format!("/proc/self/fd/{fd}")).is_err());
    #[cfg(not(target_os = "linux"))]
    let _ = fd;
}

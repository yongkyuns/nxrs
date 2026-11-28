//! Exercise the production Rust owner through fake native symbols. Neither the
//! host clock nor a host adjtime implementation is called by this witness.
use std::sync::Mutex;
use std::time::{Duration, UNIX_EPOCH};
use time::{Capabilities, Control, Error};

#[path = "../src/platform/nuttx/mod.rs"]
mod provider;

#[derive(Debug, PartialEq, Eq)]
enum Call {
    Set(i64, u32),
    Adjust(bool, i64, i32),
}

struct Native {
    admission: i32,
    rate: u32,
    status: i32,
    residual: (i64, i32),
    calls: Vec<Call>,
}

static NATIVE: Mutex<Native> = Mutex::new(Native {
    admission: 0,
    rate: 500,
    status: 0,
    residual: (0, 0),
    calls: Vec::new(),
});

#[unsafe(no_mangle)]
unsafe extern "C" fn time_control_capabilities(rate: *mut u32) -> i32 {
    let native = NATIVE.lock().unwrap();
    unsafe { *rate = native.rate };
    native.admission
}

#[unsafe(no_mangle)]
extern "C" fn time_set_realtime(seconds: i64, nanoseconds: u32) -> i32 {
    let mut native = NATIVE.lock().unwrap();
    native.calls.push(Call::Set(seconds, nanoseconds));
    if native.status == 0 {
        native.residual = (0, 0);
    }
    native.status
}

#[unsafe(no_mangle)]
unsafe extern "C" fn time_adjust_realtime(
    replace: bool,
    seconds: i64,
    microseconds: i32,
    previous_seconds: *mut i64,
    previous_microseconds: *mut i32,
) -> i32 {
    let mut native = NATIVE.lock().unwrap();
    native
        .calls
        .push(Call::Adjust(replace, seconds, microseconds));
    if native.status != 0 {
        return native.status;
    }
    unsafe {
        *previous_seconds = native.residual.0;
        *previous_microseconds = native.residual.1;
    }
    if replace {
        native.residual = (seconds, microseconds);
    }
    0
}

#[test]
fn owner_validates_and_only_changes_time_explicitly() {
    assert!(matches!(time::open(), Err(Error::Unsupported)));
    NATIVE.lock().unwrap().admission = 1;
    assert!(matches!(provider::open(), Err(Error::Unsupported)));
    NATIVE.lock().unwrap().admission = 0;

    let mut owner = provider::open().unwrap();
    assert_eq!(
        owner.capabilities(),
        Capabilities {
            slew_rate_ppm: Some(500)
        }
    );
    assert!(matches!(provider::open(), Err(Error::Busy)));
    NATIVE.lock().unwrap().rate = 0;
    assert_eq!(owner.capabilities().slew_rate_ppm, Some(500));
    NATIVE.lock().unwrap().rate = 500;

    for (value, seconds, nanoseconds) in [
        (UNIX_EPOCH, 0, 0),
        (
            UNIX_EPOCH + Duration::new(1_800_000_000, 123_456_789),
            1_800_000_000,
            123_456_789,
        ),
        (UNIX_EPOCH - Duration::from_nanos(1), -1, 999_999_999),
        (UNIX_EPOCH - Duration::from_secs(1), -1, 0),
        (UNIX_EPOCH - Duration::new(1, 123_456_789), -2, 876_543_211),
    ] {
        owner.set(value).unwrap();
        assert_eq!(
            NATIVE.lock().unwrap().calls.last(),
            Some(&Call::Set(seconds, nanoseconds))
        );
    }

    const LIMIT: i64 = i64::MAX / 1_000;
    NATIVE.lock().unwrap().residual = (-2, -5);
    let before = NATIVE.lock().unwrap().calls.len();
    for invalid in [i64::MIN, i64::MAX, LIMIT + 1, -LIMIT - 1] {
        assert_eq!(owner.replace_adjustment(invalid), Err(Error::OutOfRange));
    }
    assert_eq!(NATIVE.lock().unwrap().calls.len(), before);
    assert_eq!(owner.pending_adjustment(), Ok(-2_000_005));

    for correction in [0, 1, -1, 1_000_001, -1_000_001, LIMIT, -LIMIT] {
        NATIVE.lock().unwrap().residual = (-2, -5);
        assert_eq!(owner.replace_adjustment(correction), Ok(-2_000_005));
        assert_eq!(
            NATIVE.lock().unwrap().calls.last(),
            Some(&Call::Adjust(
                true,
                correction / 1_000_000,
                (correction % 1_000_000) as i32
            ))
        );
        assert_eq!(owner.pending_adjustment(), Ok(correction));
        assert_eq!(
            NATIVE.lock().unwrap().calls.last(),
            Some(&Call::Adjust(false, 0, 0))
        );
        assert_eq!(owner.cancel_adjustment(), Ok(correction));
        assert_eq!(owner.pending_adjustment(), Ok(0));
    }

    for invalid in [
        (1, -1),
        (-1, 1),
        (0, 1_000_000),
        (0, -1_000_000),
        (i64::MAX, 0),
        (LIMIT / 1_000_000, (LIMIT % 1_000_000 + 1) as i32),
    ] {
        NATIVE.lock().unwrap().residual = invalid;
        assert_eq!(owner.pending_adjustment(), Err(Error::Io));
    }
    NATIVE.lock().unwrap().residual = (0, 25);
    for (status, error) in [
        (1, Error::Unsupported),
        (2, Error::OutOfRange),
        (-1, Error::Io),
    ] {
        NATIVE.lock().unwrap().status = status;
        assert_eq!(owner.set(UNIX_EPOCH), Err(error));
        assert_eq!(owner.replace_adjustment(10), Err(error));
        assert_eq!(owner.pending_adjustment(), Err(error));
        assert_eq!(owner.cancel_adjustment(), Err(error));
        assert_eq!(NATIVE.lock().unwrap().residual, (0, 25));
    }
    NATIVE.lock().unwrap().status = 0;
    let before = NATIVE.lock().unwrap().calls.len();
    drop(owner);
    assert_eq!(NATIVE.lock().unwrap().calls.len(), before);
    let mut owner = provider::open().unwrap();
    assert_eq!(owner.pending_adjustment(), Ok(25));
    owner.set(UNIX_EPOCH).unwrap();
    assert_eq!(owner.pending_adjustment(), Ok(0));
    drop(owner);

    let before = NATIVE.lock().unwrap().calls.len();
    assert!(std::panic::catch_unwind(|| {
        let _owner = provider::open().unwrap();
        panic!("caller failed");
    })
    .is_err());
    assert_eq!(NATIVE.lock().unwrap().calls.len(), before);

    NATIVE.lock().unwrap().rate = 0;
    let mut owner = provider::open().unwrap();
    assert_eq!(owner.capabilities().slew_rate_ppm, None);
    owner.set(UNIX_EPOCH).unwrap();
    let before = NATIVE.lock().unwrap().calls.len();
    assert_eq!(owner.replace_adjustment(1), Err(Error::Unsupported));
    assert_eq!(owner.pending_adjustment(), Err(Error::Unsupported));
    assert_eq!(owner.cancel_adjustment(), Err(Error::Unsupported));
    assert_eq!(NATIVE.lock().unwrap().calls.len(), before);
}

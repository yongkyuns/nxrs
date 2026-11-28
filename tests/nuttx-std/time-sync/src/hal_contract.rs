use crate::target::{observe, reference, wall_ns, SECOND, SEED};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use time::{Control, Error};

pub fn run() {
    let mut clock = time::open().unwrap();
    assert_eq!(clock.capabilities().slew_rate_ppm, Some(500));
    assert!(matches!(time::open(), Err(Error::Busy)));

    // A fractional pre-epoch value exercises Rust-to-timespec normalization.
    let mono = Instant::now();
    let before = reference();
    clock.set(UNIX_EPOCH - Duration::from_millis(250)).unwrap();
    let wall = wall_ns(SystemTime::now());
    let after = reference();
    assert!(wall >= -250_000_000 && wall <= -250_000_000 + (after - before) as i64 + 200_000);
    assert!(Instant::now().checked_duration_since(mono).is_some());
    clock.set(UNIX_EPOCH + Duration::from_secs(SEED)).unwrap();
    println!("TIME_SYNC {{\"kind\":\"hal_admission\",\"slew_rate_ppm\":500,\"exclusive\":true,\"pre_epoch\":true}}");

    for (name, request) in [("positive", 2_000_i64), ("negative", -2_000)] {
        assert_eq!(clock.replace_adjustment(request).unwrap(), 0);
        let requested = reference();
        observe(name, Duration::from_millis(1_250), request);
        let before = reference();
        let pending = clock.pending_adjustment().unwrap();
        let after = reference();
        let elapsed = before - requested;
        let uncertainty = 2 + (after - before + 200_000) / 2_000_000;
        let expected = request.signum() * (request.abs() - (elapsed / 2_000_000) as i64).max(0);
        assert_eq!(pending.signum(), request.signum());
        assert!((pending - expected).unsigned_abs() <= uncertainty);
        let cancelled = clock.cancel_adjustment().unwrap();
        assert_eq!(cancelled.signum(), request.signum());
        assert!(cancelled.abs() <= pending.abs());
        assert_eq!(clock.pending_adjustment().unwrap(), 0);
        println!("TIME_SYNC {{\"kind\":\"hal_pending\",\"name\":\"{name}\",\"request_us\":{request},\"elapsed_ns\":{elapsed},\"pending_us\":{pending},\"uncertainty_us\":{uncertainty},\"cancelled_us\":{cancelled}}}");
    }

    // Whole seconds and a negative fraction traverse the production C bridge.
    for request in [2_500_123_i64, -2_500_123] {
        clock.replace_adjustment(request).unwrap();
        assert_eq!(clock.replace_adjustment(i64::MAX), Err(Error::OutOfRange));
        let pending = clock.pending_adjustment().unwrap();
        assert_eq!(pending.signum(), request.signum());
        assert!((pending - request).unsigned_abs() < 10);
        clock.cancel_adjustment().unwrap();
        println!("TIME_SYNC {{\"kind\":\"hal_range\",\"request_us\":{request},\"pending_us\":{pending},\"rejected_unchanged\":true}}");
    }

    clock.replace_adjustment(2_000).unwrap();
    drop(clock);
    let mut clock = time::open().unwrap();
    assert!(clock.pending_adjustment().unwrap() > 1_900);
    clock.set(UNIX_EPOCH + Duration::from_secs(SEED)).unwrap();
    assert_eq!(clock.pending_adjustment().unwrap(), 0);
    drop(clock);
    println!("TIME_SYNC {{\"kind\":\"hal_lifecycle\",\"reopened\":true,\"drop_preserved\":true,\"set_cleared\":true,\"reference_elapsed_ns\":{}}}", reference());
    assert!(reference() < 10 * SECOND);
}

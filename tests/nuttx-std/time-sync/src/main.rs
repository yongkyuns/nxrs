//! Target-only integration witness for the production time controller.
#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("time_sync requires its isolated NuttX QEMU image");
    std::process::exit(2);
}

#[cfg(target_os = "nuttx")]
fn main() {
    target::run();
}

#[cfg(all(target_os = "nuttx", feature = "controller"))]
mod controller_contract;
#[cfg(all(target_os = "nuttx", feature = "controller"))]
mod hal_contract;

#[cfg(target_os = "nuttx")]
mod target {
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    extern "C" {
        fn timing_clock_initialize() -> i32;
        fn timing_clock_stop();
        fn time_reference_ns() -> u64;
    }

    pub const SECOND: u64 = 1_000_000_000;
    pub const TICK_NS: u64 = 100_000;
    pub const SEED: u64 = 1_800_000_000;

    pub fn reference() -> u64 {
        let value = unsafe { time_reference_ns() };
        assert!(value < 60 * SECOND, "independent counter lifetime exceeded");
        value
    }

    pub fn wall_ns(time: SystemTime) -> i64 {
        match time.duration_since(UNIX_EPOCH) {
            Ok(elapsed) => i64::try_from(elapsed.as_nanos()).unwrap(),
            Err(before) => -i64::try_from(before.duration().as_nanos()).unwrap(),
        }
    }

    #[derive(Clone, Copy)]
    pub struct Read {
        pub wall: i64,
        pub mono: u64,
        pub reference: u64,
        pub span: u64,
    }

    pub fn read(origin: Instant) -> Read {
        let before = reference();
        let mono = Instant::now()
            .checked_duration_since(origin)
            .unwrap()
            .as_nanos() as u64;
        let wall = wall_ns(SystemTime::now());
        let after = reference();
        Read {
            wall,
            mono,
            reference: before + (after - before) / 2,
            span: after - before,
        }
    }

    /// Dense reads check the real std clocks independently of the controller.
    pub fn observe(name: &str, duration: Duration, request_us: i64) {
        let origin = Instant::now();
        let first = read(origin);
        let mut last = first;
        let mut reads = 0_u64;
        let mut max_span = first.span;
        let mut max_bound = 0;
        loop {
            let next = read(origin);
            assert!(next.reference >= last.reference);
            assert!(next.mono >= last.mono, "monotonic clock reversed");
            assert!(next.wall >= last.wall, "wall clock reversed during slew");
            let bound =
                2 * TICK_NS + last.span + next.span + (next.reference - last.reference) / 2_000;
            let correction = (next.wall - last.wall) - (next.mono - last.mono) as i64;
            assert!(correction.unsigned_abs() <= bound, "wall correction jumped");
            reads += 1;
            max_span = max_span.max(next.span);
            max_bound = max_bound.max(bound);
            last = next;
            if last.reference - first.reference >= duration.as_nanos() as u64 {
                break;
            }
        }
        let reference_ns = last.reference - first.reference;
        let monotonic_ns = last.mono - first.mono;
        let wall_ns = last.wall - first.wall;
        let uncertainty_ns = 2 * TICK_NS + first.span + last.span;
        let expected = request_us.signum()
            * (request_us.unsigned_abs() * 1_000).min(reference_ns / 2_000) as i64;
        assert!((monotonic_ns as i64 - reference_ns as i64).unsigned_abs() <= uncertainty_ns);
        assert!((wall_ns - monotonic_ns as i64 - expected).unsigned_abs() <= uncertainty_ns);
        assert!(
            max_bound < 500_000,
            "observation cannot exclude the old staircase"
        );
        println!("TIME_SYNC {{\"kind\":\"observation\",\"name\":\"{name}\",\"request_us\":{request_us},\"reference_ns\":{reference_ns},\"monotonic_ns\":{monotonic_ns},\"wall_ns\":{wall_ns},\"uncertainty_ns\":{uncertainty_ns},\"reads\":{reads},\"max_read_span_ns\":{max_span},\"max_pair_bound_ns\":{max_bound}}}");
    }

    pub fn run() {
        assert_eq!(unsafe { timing_clock_initialize() }, 0);
        let variant = if cfg!(feature = "controller") {
            "controller"
        } else {
            "baseline"
        };
        println!("TIME_SYNC {{\"kind\":\"begin\",\"schema\":1,\"variant\":\"{variant}\",\"tick_us\":100}}");
        observe("baseline", Duration::from_millis(250), 0);
        #[cfg(feature = "controller")]
        {
            crate::hal_contract::run();
            crate::controller_contract::run();
        }
        let elapsed = reference();
        unsafe { timing_clock_stop() };
        println!("TIME_SYNC {{\"kind\":\"complete\",\"schema\":1,\"variant\":\"{variant}\",\"reference_elapsed_ns\":{elapsed}}}");
    }
}

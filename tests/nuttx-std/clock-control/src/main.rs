//! Characterize the actual OS clock; only the host oracle decides qualification.
#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("clock_probe only runs inside its NuttX QEMU image");
    std::process::exit(2);
}

#[cfg(target_os = "nuttx")]
fn main() {
    target::run();
}

#[cfg(target_os = "nuttx")]
mod target {
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    extern "C" {
        fn timing_clock_initialize() -> i32;
        fn timing_clock_stop();
        fn clock_probe_reference_ns() -> u64;
        fn clock_probe_features() -> u32;
        fn clock_probe_tick_us() -> u32;
        fn clock_probe_resolution_ns() -> i64;
        fn clock_probe_set(nanoseconds: i64, error: *mut i32) -> i32;
        fn clock_probe_adjust(
            present: i32,
            microseconds: i64,
            old_seconds: *mut i64,
            old_microseconds: *mut i64,
            error: *mut i32,
        ) -> i32;
    }

    const SECOND: u64 = 1_000_000_000;
    const QUARTER: u64 = SECOND / 4;
    const SEED: i64 = 1_800_000_000_000_000_000;

    #[derive(Clone, Copy, Default)]
    struct Reading {
        realtime: i64,
        monotonic: i64,
        reference: u64,
        span: u64,
    }

    fn read(origin: Instant) -> Reading {
        let begin = unsafe { clock_probe_reference_ns() };
        let instant = Instant::now();
        let realtime = match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(value) => i64::try_from(value.as_nanos()).unwrap(),
            Err(error) => -i64::try_from(error.duration().as_nanos()).unwrap(),
        };
        let end = unsafe { clock_probe_reference_ns() };
        assert!(
            end >= begin && end < 60 * SECOND,
            "reference wrapped or suite exceeded 60 s"
        );
        let monotonic = match instant.checked_duration_since(origin) {
            Some(value) => i64::try_from(value.as_nanos()).unwrap(),
            None => -i64::try_from(origin.duration_since(instant).as_nanos()).unwrap(),
        };
        Reading {
            realtime,
            monotonic,
            reference: begin + (end - begin) / 2,
            span: end - begin,
        }
    }

    fn sample(phase: &str, point: &str, index: usize, value: Reading) {
        println!("CLOCK_PROBE {{\"kind\":\"sample\",\"phase\":\"{phase}\",\"point\":\"{point}\",\"index\":{index},\"realtime_ns\":{},\"monotonic_ns\":{},\"reference_ns\":{},\"read_span_ns\":{}}}",
                 value.realtime, value.monotonic, value.reference, value.span);
    }

    // Fixed stack storage; the dense observation interval performs no I/O or allocation.
    fn phase(name: &str, milliseconds: u64, origin: Instant) {
        let first = read(origin);
        let mut previous = first;
        let mut points = [Reading::default(); 32];
        let mut used = 0;
        let mut next = first.reference + QUARTER;
        let mut count = 0_u64;
        let mut realtime_regressions = 0_u64;
        let mut monotonic_regressions = 0_u64;
        let mut minimum_delta = i64::MAX;
        let mut realtime_resolution = i64::MAX;
        let mut monotonic_resolution = i64::MAX;
        let mut worst_span = first.span;
        let mut regression = None;
        while previous.reference - first.reference < milliseconds * 1_000_000 {
            let current = read(origin);
            assert!(
                current.reference >= previous.reference,
                "reference went backward"
            );
            let delta = current.realtime - previous.realtime;
            minimum_delta = minimum_delta.min(delta);
            if delta < 0 {
                realtime_regressions += 1;
                regression.get_or_insert((previous, current));
            } else if delta > 0 {
                realtime_resolution = realtime_resolution.min(delta);
            }
            let mono_delta = current.monotonic - previous.monotonic;
            if mono_delta < 0 {
                monotonic_regressions += 1;
            } else if mono_delta > 0 {
                monotonic_resolution = monotonic_resolution.min(mono_delta);
            }
            worst_span = worst_span.max(current.span);
            if current.reference >= next {
                assert!(used < points.len(), "checkpoint capacity exceeded");
                points[used] = current;
                used += 1;
                next = current.reference + QUARTER;
            }
            count += 1;
            previous = current;
            std::hint::spin_loop();
        }
        sample(name, "start", 0, first);
        for (index, point) in points[..used].iter().enumerate() {
            sample(name, "checkpoint", index, *point);
        }
        sample(name, "end", 0, previous);
        if let Some((before, after)) = regression {
            sample(name, "regression_before", 0, before);
            sample(name, "regression_after", 0, after);
        }
        println!("CLOCK_PROBE {{\"kind\":\"phase\",\"name\":\"{name}\",\"duration_ms\":{milliseconds},\"reads\":{count},\"realtime_regressions\":{realtime_regressions},\"monotonic_regressions\":{monotonic_regressions},\"min_realtime_delta_ns\":{minimum_delta},\"min_positive_realtime_delta_ns\":{},\"min_positive_monotonic_delta_ns\":{},\"max_read_span_ns\":{worst_span}}}",
                 if realtime_resolution == i64::MAX { 0 } else { realtime_resolution },
                 if monotonic_resolution == i64::MAX { 0 } else { monotonic_resolution });
    }

    fn set(name: &str, target: i64, origin: Instant) {
        let before = read(origin);
        let mut error = 0;
        let result = unsafe { clock_probe_set(target, &mut error) };
        let after = read(origin);
        sample(name, "before", 0, before);
        sample(name, "after", 0, after);
        println!("CLOCK_PROBE {{\"kind\":\"set\",\"name\":\"{name}\",\"target_ns\":{target},\"result\":{result},\"errno\":{error}}}");
        assert_eq!(result, 0, "clock_settime failed");
    }

    fn adjust(name: &str, delta: Option<i64>, origin: Instant) {
        let before = read(origin);
        let (mut seconds, mut microseconds, mut error) = (0, 0, 0);
        let result = unsafe {
            clock_probe_adjust(
                i32::from(delta.is_some()),
                delta.unwrap_or(0),
                &mut seconds,
                &mut microseconds,
                &mut error,
            )
        };
        let after = read(origin);
        sample(name, "before", 0, before);
        sample(name, "after", 0, after);
        println!("CLOCK_PROBE {{\"kind\":\"adjust\",\"name\":\"{name}\",\"has_delta\":{},\"delta_us\":{},\"result\":{result},\"errno\":{error},\"old_seconds\":{seconds},\"old_microseconds\":{microseconds}}}",
                 delta.is_some(), delta.unwrap_or(0));
        if delta.is_some() {
            assert_eq!(result, 0, "adjtime request failed");
        }
    }

    fn sleep(name: &str, origin: Instant) {
        let before = read(origin);
        std::thread::sleep(Duration::from_millis(100));
        let after = read(origin);
        sample(name, "before", 0, before);
        sample(name, "after", 0, after);
        println!(
            "CLOCK_PROBE {{\"kind\":\"sleep\",\"name\":\"{name}\",\"requested_ns\":100000000}}"
        );
    }

    pub(super) fn run() {
        assert_eq!(
            unsafe { timing_clock_initialize() },
            0,
            "APB timer unavailable"
        );
        let origin = Instant::now();
        let features = unsafe { clock_probe_features() };
        let tick_us = unsafe { clock_probe_tick_us() };
        let resolution = unsafe { clock_probe_resolution_ns() };
        let build = option_env!("CLOCK_PROBE_BUILD_ID").unwrap_or("unbound");
        println!("CLOCK_PROBE {{\"kind\":\"begin\",\"schema\":1,\"build_id\":\"{build}\",\"timekeeping\":{},\"adjtime_header\":{},\"rtc\":{},\"rtc_hires\":{},\"direct_implementation_only\":{},\"tick_us\":{tick_us},\"reported_realtime_resolution_ns\":{resolution},\"reference_hz\":20000000}}",
                 features & 1 != 0, features & 2 != 0, features & 4 != 0,
                 features & 8 != 0, features & 3 == 1);
        set("seed", SEED, origin);
        set("forward", read(origin).realtime + 3 * SECOND as i64, origin);
        set(
            "backward",
            read(origin).realtime - 2 * SECOND as i64,
            origin,
        );
        sleep("baseline_sleep", origin);
        phase("baseline", 500, origin);
        adjust("initial_query", None, origin);
        let mut phases = 1;
        if features & 3 != 0 {
            set("positive_seed", SEED + 10 * SECOND as i64, origin);
            adjust("positive", Some(2_000), origin);
            sleep("positive_sleep", origin);
            phase("positive", 5_000, origin);
            adjust("positive_query", None, origin);
            set("negative_seed", SEED + 20 * SECOND as i64, origin);
            adjust("negative", Some(-2_000), origin);
            sleep("negative_sleep", origin);
            phase("negative", 5_000, origin);
            adjust("negative_query", None, origin);
            set("replace_seed", SEED + 30 * SECOND as i64, origin);
            adjust("replace_initial", Some(20_000), origin);
            phase("before_replace", 1_200, origin);
            adjust("replacement", Some(-2_000), origin);
            phase("replaced", 5_000, origin);
            set("cancel_seed", SEED + 40 * SECOND as i64, origin);
            adjust("cancel_initial", Some(20_000), origin);
            phase("before_cancel", 1_200, origin);
            adjust("cancel", Some(0), origin);
            phase("cancelled", 2_000, origin);
            adjust("final_query", None, origin);
            phases = 7;
        }
        let elapsed = unsafe { clock_probe_reference_ns() };
        unsafe { timing_clock_stop() };
        println!("CLOCK_PROBE {{\"kind\":\"complete\",\"schema\":1,\"phases\":{phases},\"reference_elapsed_ns\":{elapsed}}}");
    }
}

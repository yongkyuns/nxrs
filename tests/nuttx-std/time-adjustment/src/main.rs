//! Public NuttX adjustment contract, measured against the independent APB timer.
#[cfg(not(target_os = "nuttx"))]
fn main() {
    eprintln!("time_adjustment requires its isolated NuttX QEMU image");
    std::process::exit(2);
}

#[cfg(target_os = "nuttx")]
fn main() {
    target::run();
}

#[cfg(target_os = "nuttx")]
mod target {
    use std::sync::mpsc;
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    extern "C" {
        fn timing_clock_initialize() -> i32;
        fn timing_clock_stop();
        fn clock_probe_reference_ns() -> u64;
        fn clock_probe_features() -> u32;
        fn clock_probe_tick_us() -> u32;
        fn clock_probe_set(nanoseconds: i64, error: *mut i32) -> i32;
        fn clock_probe_adjust(
            present: i32,
            us: i64,
            sec: *mut i64,
            usec: *mut i64,
            error: *mut i32,
        ) -> i32;
    }

    const SECOND: u64 = 1_000_000_000;
    const SEED: i64 = 1_800_000_000_000_000_000;

    fn reference() -> u64 {
        let value = unsafe { clock_probe_reference_ns() };
        assert!(value < 60 * SECOND, "reference lifetime exceeded");
        value
    }

    #[derive(Clone, Copy)]
    struct Sample {
        wall: i64,
        mono: i64,
        reference: u64,
        span: u64,
    }

    fn read(origin: Instant) -> Sample {
        let before = reference();
        let mono = Instant::now()
            .checked_duration_since(origin)
            .expect("Instant reversed past origin")
            .as_nanos() as i64;
        let wall = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos() as i64;
        let after = reference();
        assert!(after >= before);
        Sample {
            wall,
            mono,
            reference: before + (after - before) / 2,
            span: after - before,
        }
    }

    fn set(time: i64) {
        let mut error = 0;
        assert_eq!(unsafe { clock_probe_set(time, &mut error) }, 0);
        assert_eq!(error, 0);
    }

    // The shared mechanical C helper consumes the actual public sys/time.h
    // declaration in this image. Both output fields must be written/coherent.
    fn adjust(delta: Option<i64>) -> i64 {
        let origin = Instant::now();
        let first = read(origin);
        let (mut sec, mut usec, mut error) = (0, 0, 0);
        assert_eq!(
            unsafe {
                clock_probe_adjust(
                    i32::from(delta.is_some()),
                    delta.unwrap_or(0),
                    &mut sec,
                    &mut usec,
                    &mut error,
                )
            },
            0
        );
        assert_eq!(error, 0);
        let last = read(origin);
        let elapsed = last.reference - first.reference;
        let monotonic = last.mono - first.mono;
        let wall = last.wall - first.wall;
        let uncertainty =
            u64::from(unsafe { clock_probe_tick_us() }) * 2000 + first.span + last.span;
        // A control call may advance either pending request at 500 ppm, but
        // must never introduce an additional wall/monotonic discontinuity.
        // Assert locally without logging or allocating in the dense loop.
        assert!(
            elapsed < 10_000_000,
            "control observation exceeded fixture budget"
        );
        assert!(
            uncertainty + elapsed.div_ceil(2000) < 1_000_000,
            "control observation cannot distinguish a clock discontinuity"
        );
        assert!((monotonic - elapsed as i64).unsigned_abs() <= uncertainty);
        assert!((wall - monotonic).unsigned_abs() <= uncertainty + elapsed.div_ceil(2000));
        assert!(usec > -1_000_000 && usec < 1_000_000);
        assert!(sec == 0 || usec == 0 || sec.signum() == usec.signum());
        assert!(
            sec.unsigned_abs() <= 3,
            "residual exceeds every request in this fixture"
        );
        sec * 1_000_000 + usec
    }

    fn request(delta: i64) -> (i64, u64, u64) {
        let before = reference();
        let old = adjust(Some(delta));
        let after = reference();
        (old, before + (after - before) / 2, after - before)
    }

    #[derive(Clone, Copy, Default)]
    struct Pending {
        elapsed: u64,
        remaining: i64,
        uncertainty_us: u64,
    }

    fn pending(delta: i64, epoch: u64, request_span: u64, tick: u64) -> Pending {
        let before = reference();
        let remaining = adjust(None);
        let after = reference();
        let elapsed = before + (after - before) / 2 - epoch;
        // 500 ppm = one microsecond of correction per two milliseconds.
        let expected = (delta.abs() - (elapsed / 2_000_000) as i64).max(0) * delta.signum();
        let uncertainty_us = (tick * 1000 + request_span + after - before).div_ceil(2_000_000) + 1;
        assert!(
            (remaining - expected).unsigned_abs() <= uncertainty_us,
            "elapsed-time slew budget mismatch"
        );
        Pending {
            elapsed,
            remaining,
            uncertainty_us,
        }
    }

    fn observe(name: &str, delta: i64, epoch: u64, request_span: u64, origin: Instant, tick: u64) {
        let first = read(origin);
        let mut last = first;
        let mut reads = 0;
        let (mut wall_backwards, mut mono_backwards) = (0, 0);
        let mut minimum = i64::MAX;
        let mut max_span = first.span;
        let mut slew_discontinuities = 0;
        let mut max_slew_excess_ns = 0;
        let mut max_slew_uncertainty_ns = 0;
        let mut points = [Pending::default(); 4];
        let mut used = 0;
        let duration = 4_500_000_000;
        while last.reference - first.reference < duration {
            let now = read(origin);
            assert!(now.reference >= last.reference);
            let change = now.wall - last.wall;
            wall_backwards += usize::from(change < 0);
            mono_backwards += usize::from(now.mono < last.mono);
            let slew_uncertainty = tick * 2000
                + last.span
                + now.span
                + (now.reference - last.reference).div_ceil(2000);
            let slew_change = (change - (now.mono - last.mono)).unsigned_abs();
            let excess = slew_change.saturating_sub(slew_uncertainty);
            slew_discontinuities += usize::from(excess != 0);
            max_slew_excess_ns = max_slew_excess_ns.max(excess);
            max_slew_uncertainty_ns = max_slew_uncertainty_ns.max(slew_uncertainty);
            minimum = minimum.min(change);
            max_span = max_span.max(now.span);
            if used < points.len() && now.reference - epoch >= (used as u64 + 1) * SECOND {
                points[used] = pending(delta, epoch, request_span, tick);
                used += 1;
            }
            last = now;
            reads += 1;
        }
        let remaining = adjust(None);
        let reference_ns = last.reference - first.reference;
        let monotonic_ns = last.mono - first.mono;
        let wall_ns = last.wall - first.wall;
        let uncertainty_ns = tick * 2000 + first.span + last.span;
        println!("TIME_ADJUSTMENT {{\"kind\":\"phase\",\"name\":\"{name}\",\"request_us\":{delta},\"reference_ns\":{reference_ns},\"monotonic_ns\":{monotonic_ns},\"wall_ns\":{wall_ns},\"uncertainty_ns\":{uncertainty_ns},\"reads\":{reads},\"realtime_regressions\":{wall_backwards},\"monotonic_regressions\":{mono_backwards},\"min_realtime_delta_ns\":{minimum},\"max_read_span_ns\":{max_span},\"pending_us\":{remaining},\"slew_discontinuities\":{slew_discontinuities},\"max_slew_excess_ns\":{max_slew_excess_ns},\"max_slew_uncertainty_ns\":{max_slew_uncertainty_ns}}}");
        for (index, p) in points[..used].iter().enumerate() {
            println!("TIME_ADJUSTMENT {{\"kind\":\"pending\",\"phase\":\"{name}\",\"index\":{index},\"elapsed_ns\":{},\"remaining_us\":{},\"uncertainty_us\":{}}}", p.elapsed, p.remaining, p.uncertainty_us);
        }
        assert_eq!((wall_backwards, mono_backwards, remaining), (0, 0, 0));
        assert_eq!(slew_discontinuities, 0);
        assert!(
            max_slew_uncertainty_ns < 500_000,
            "sample resolution cannot distinguish the original 500us staircase"
        );
        assert!((monotonic_ns - reference_ns as i64).unsigned_abs() <= uncertainty_ns);
        if delta != 0 {
            assert!(
                uncertainty_ns < delta.unsigned_abs() * 500,
                "clock resolution cannot distinguish the requested correction"
            );
        }
        assert!((wall_ns - monotonic_ns - delta * 1000).unsigned_abs() <= uncertainty_ns);
    }

    fn wait(name: &str, action: i64, is_step: bool, origin: Instant, tick: u64) {
        set(SEED);
        let requested_ms = if is_step { 250 } else { 1250 };
        let (sender, receiver) = mpsc::sync_channel(0);
        let worker = thread::spawn(move || {
            let first = read(origin);
            sender.send(first).unwrap();
            thread::sleep(Duration::from_millis(requested_ms));
            (first, read(origin))
        });
        let started = receiver.recv().unwrap();
        thread::sleep(Duration::from_millis(50));
        let action_reference = reference();
        let request_epoch;
        let request_span;
        if is_step {
            let before = reference();
            set(read(origin).wall + action);
            (request_epoch, request_span) = (0, reference() - before);
        } else {
            let (old, epoch, span) = request(action);
            assert_eq!(old, 0);
            (request_epoch, request_span) = (epoch, span);
        }
        let action_completed = reference();
        let (first, last) = worker.join().unwrap();
        assert_eq!(first.reference, started.reference);
        assert!(first.reference < action_reference && action_reference < last.reference);
        assert!(action_reference <= action_completed && action_completed < last.reference);
        let reference_ns = last.reference - first.reference;
        let monotonic_ns = last.mono - first.mono;
        let wall_ns = last.wall - first.wall;
        let requested_ns = requested_ms * 1_000_000;
        let uncertainty_ns = tick * 2000 + first.span + last.span;
        let progress = if is_step {
            Pending {
                remaining: adjust(None),
                ..Pending::default()
            }
        } else {
            pending(action, request_epoch, request_span, tick)
        };
        let remaining = progress.remaining;
        let request_offset_ns = if is_step {
            0
        } else {
            request_epoch - first.reference
        };
        let action_completed_offset_ns = action_completed - first.reference;
        println!("TIME_ADJUSTMENT {{\"kind\":\"wait\",\"name\":\"{name}\",\"step\":{is_step},\"action\":{action},\"requested_ns\":{requested_ns},\"reference_ns\":{reference_ns},\"monotonic_ns\":{monotonic_ns},\"wall_ns\":{wall_ns},\"uncertainty_ns\":{uncertainty_ns},\"action_offset_ns\":{},\"action_completed_offset_ns\":{action_completed_offset_ns},\"request_offset_ns\":{request_offset_ns},\"request_span_ns\":{request_span},\"pending_elapsed_ns\":{},\"pending_uncertainty_us\":{},\"pending_us\":{remaining}}}", action_reference - first.reference, progress.elapsed, progress.uncertainty_us);
        assert!(reference_ns + tick * 1000 >= requested_ns);
        assert!(reference_ns <= requested_ns + requested_ns / 50 + uncertainty_ns);
        assert!((monotonic_ns - reference_ns as i64).unsigned_abs() <= uncertainty_ns);
        if is_step {
            assert_eq!(remaining, 0);
            assert!(
                (wall_ns - monotonic_ns - action).unsigned_abs() <= uncertainty_ns + request_span
            );
        } else {
            assert!(remaining.signum() == action.signum() && remaining.abs() < action.abs() - 500);
            let applied_ns = ((last.reference - request_epoch) / 2000)
                .min(action.unsigned_abs() * 1000) as i64
                * action.signum();
            assert!(
                uncertainty_ns + request_span < applied_ns.unsigned_abs(),
                "clock resolution cannot distinguish the in-wait correction"
            );
            assert!(
                (wall_ns - monotonic_ns - applied_ns).unsigned_abs()
                    <= uncertainty_ns + request_span
            );
        }
        adjust(Some(0));
    }

    pub(super) fn run() {
        assert_eq!(unsafe { timing_clock_initialize() }, 0);
        let origin = Instant::now();
        let features = unsafe { clock_probe_features() };
        let tick = u64::from(unsafe { clock_probe_tick_us() });
        println!("TIME_ADJUSTMENT {{\"kind\":\"begin\",\"schema\":1,\"features\":{features},\"tick_us\":{tick}}}");
        assert_eq!(
            features, 3,
            "requires public software adjustment, without RTC"
        );
        for (name, delta, fraction) in [
            ("baseline", 0, 0),
            ("positive", 2000, 0),
            ("negative", -2000, 0),
            ("near_second", -2000, 999_900_000),
        ] {
            set(SEED + fraction);
            assert_eq!(adjust(None), 0);
            let (old, epoch, span) = request(delta);
            assert_eq!(old, 0);
            observe(name, delta, epoch, span, origin, tick);
        }
        set(SEED);
        let (_, old_epoch, old_span) = request(20_000);
        thread::sleep(Duration::from_millis(1100));
        let before = pending(20_000, old_epoch, old_span, tick).remaining;
        let (old, epoch, span) = request(-2000);
        assert!((old - before).abs() <= 2);
        observe("replacement", -2000, epoch, span, origin, tick);
        set(SEED);
        let (_, old_epoch, old_span) = request(20_000);
        thread::sleep(Duration::from_millis(1100));
        let before = pending(20_000, old_epoch, old_span, tick).remaining;
        let (old, epoch, span) = request(0);
        assert!((old - before).abs() <= 2);
        observe("cancelled", 0, epoch, span, origin, tick);
        for delta in [2_500_123_i64, -2_500_123] {
            let (old, epoch, span) = request(delta);
            assert_eq!(old, 0);
            let remaining = pending(delta, epoch, span, tick).remaining;
            let cancelled = adjust(Some(0));
            assert!((cancelled - remaining).abs() <= 2);
            println!("TIME_ADJUSTMENT {{\"kind\":\"large\",\"request_us\":{delta},\"pending_us\":{remaining},\"cancelled_us\":{cancelled}}}");
        }
        request(20_000);
        set(SEED);
        assert_eq!(adjust(None), 0);
        println!("TIME_ADJUSTMENT {{\"kind\":\"set_clears_adjustment\",\"pending_us\":0}}");
        wait("positive", 2000, false, origin, tick);
        wait("negative", -2000, false, origin, tick);
        wait("forward", 3 * SECOND as i64, true, origin, tick);
        wait("backward", -(3 * SECOND as i64), true, origin, tick);
        let elapsed = reference();
        unsafe { timing_clock_stop() };
        println!("TIME_ADJUSTMENT {{\"kind\":\"complete\",\"schema\":1,\"phases\":6,\"waits\":4,\"large\":2,\"reference_elapsed_ns\":{elapsed}}}");
    }
}

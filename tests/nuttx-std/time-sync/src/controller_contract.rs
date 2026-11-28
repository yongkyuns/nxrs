use crate::target::{observe, read, reference, SEED, TICK_NS};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use time_sync::{
    Controller, Epoch, Error, InitialTime, Observation, Policy, Source, State, Status,
};

fn observation(epoch: Epoch, correction_us: i64) -> Observation {
    let at = Instant::now();
    let current = SystemTime::now();
    let correction = Duration::from_micros(correction_us.unsigned_abs());
    Observation {
        source: 0,
        epoch,
        at,
        utc: if correction_us < 0 {
            current - correction
        } else {
            current + correction
        },
        uncertainty: Duration::from_micros(200),
    }
}

fn state_name(state: State) -> &'static str {
    match state {
        State::Unknown => "Unknown",
        State::Checking => "Checking",
        State::Adjusting => "Adjusting",
        State::Synchronized => "Synchronized",
        State::Holdover => "Holdover",
        State::StepRequired => "StepRequired",
        State::Conflict => "Conflict",
        State::Fault => "Fault",
        State::Suspended => "Suspended",
    }
}

fn report(name: &str, status: Status) {
    let state = state_name(status.state);
    let initialized = status.initialized;
    let age_ns = status
        .reference_age
        .map_or(-1, |value| value.as_nanos() as i64);
    let bound_ns = status
        .error_bound
        .map_or(-1, |value| value.as_nanos() as i64);
    let source = status.source.map_or(-1, |value| value as i64);
    let pending_known = status.pending_us.is_some();
    let pending_us = status.pending_us.unwrap_or(0);
    println!("TIME_SYNC {{\"kind\":\"controller\",\"name\":\"{name}\",\"state\":\"{state}\",\"initialized\":{initialized},\"source\":{source},\"age_ns\":{age_ns},\"bound_ns\":{bound_ns},\"pending_known\":{pending_known},\"pending_us\":{pending_us}}}");
}

pub fn run() {
    let clock = time::open().unwrap();
    let policy = Policy {
        accuracy: Duration::from_millis(1),
        refresh_interval: Duration::from_secs(6),
        max_holdover: Duration::from_secs(12),
        max_slew_time: Duration::from_secs(10),
        clock_resolution: Duration::from_micros(100),
        drift_ppm: 10,
    };
    let mut controller = Controller::new(
        clock,
        policy,
        [Source { priority: 1 }],
        InitialTime::Unknown,
    )
    .unwrap();
    assert!(matches!(controller.last_status().state, State::Unknown));
    let seeded = controller
        .observe(Observation {
            source: 0,
            epoch: controller.epoch(),
            at: Instant::now(),
            utc: UNIX_EPOCH + Duration::from_secs(SEED + 10),
            uncertainty: Duration::from_micros(200),
        })
        .unwrap();
    assert!(matches!(seeded.state, State::Checking));
    report("seed", seeded);
    let checked = controller.poll().unwrap();
    assert!(matches!(checked.state, State::Synchronized));
    report("seed_checked", checked);

    for (name, offset) in [
        ("controller_positive", 2_000_i64),
        ("controller_negative", -2_000),
    ] {
        if offset < 0 {
            // Replace a still-active positive correction with a fresh negative
            // offset. The owner must not add/subtract the old residual again.
            controller
                .observe(observation(controller.epoch(), 2_000))
                .unwrap();
            thread::sleep(Duration::from_secs(1));
            let active = controller.poll().unwrap();
            assert!(matches!(active.state, State::Adjusting));
            assert!((1_000..=1_800).contains(&active.pending_us.unwrap()));
            report("before_replacement", active);
        }
        controller
            .observe(observation(controller.epoch(), offset))
            .unwrap();
        let status = controller.poll().unwrap();
        assert!(matches!(status.state, State::Adjusting));
        let request = status.pending_us.unwrap();
        assert!(request.signum() == offset.signum() && (1_800..=2_200).contains(&request.abs()));
        report(name, status);
        observe(name, Duration::from_millis(4_500), request);
        let settled = controller.poll().unwrap();
        assert!(matches!(settled.state, State::Synchronized));
        assert!(settled.pending_us == Some(0));
        report(
            if offset > 0 {
                "positive_settled"
            } else {
                "negative_settled"
            },
            settled,
        );
    }

    // A rejected reordered observation must not step or replace the clock.
    let mut old = observation(controller.epoch(), 3_000_000);
    old.at -= Duration::from_secs(5);
    let origin = Instant::now();
    let first = read(origin);
    assert!(matches!(controller.observe(old), Err(Error::Reordered)));
    let last = read(origin);
    let correction = (last.wall - first.wall) - (last.mono - first.mono) as i64;
    assert!(correction.unsigned_abs() <= 2 * TICK_NS + first.span + last.span);
    println!("TIME_SYNC {{\"kind\":\"rejected\",\"name\":\"reordered\",\"unchanged\":true}}");

    thread::sleep(Duration::from_secs(2));
    let held = controller.poll().unwrap();
    assert!(matches!(held.state, State::Holdover));
    report("holdover", held);
    controller
        .observe(observation(controller.epoch(), 0))
        .unwrap();
    let refreshed = controller.poll().unwrap();
    assert!(matches!(refreshed.state, State::Synchronized));
    report("refreshed", refreshed);

    let old_epoch = controller.epoch();
    controller.suspend().unwrap();
    assert!(matches!(controller.last_status().state, State::Suspended));
    let resumed_epoch = controller.resume().unwrap();
    assert!(resumed_epoch != old_epoch);
    assert!(matches!(
        controller.observe(observation(old_epoch, 3_000_000)),
        Err(Error::WrongEpoch)
    ));
    controller.observe(observation(resumed_epoch, 0)).unwrap();
    let resumed = controller.poll().unwrap();
    assert!(matches!(resumed.state, State::Synchronized));
    report("resumed", resumed);
    println!("TIME_SYNC {{\"kind\":\"lifecycle\",\"old_epoch_rejected\":true,\"controller_bytes\":{},\"reference_elapsed_ns\":{}}}", std::mem::size_of_val(&controller), reference());
}

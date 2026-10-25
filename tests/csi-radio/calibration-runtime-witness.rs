//! Deterministic host counterexamples for the existing experimental runtime.
//! These reproduce undesirable outcomes; they do not qualify a corrected policy.
#![allow(dead_code)]
#[path = "presence/src/calibration.rs"]
mod calibration;
#[path = "presence/src/diagnostics.rs"]
mod diagnostics;
use calibration::{Config, Feature, Phase, Runtime, State};
use std::sync::Barrier;
fn feature(sequence: u32) -> Feature {
    let us = u64::from(sequence) * 10_000;
    Feature {
        sequence,
        oldest_us: us,
        newest_us: us,
        templates: 10,
        training_complete: true,
        gain_available: true,
        wander: 1.0 / 1024.0,
    }
}
fn ready() -> Runtime {
    let runtime = Runtime::new();
    assert!(runtime.prepare(Config::EMPTY_HOLDOUT));
    assert_eq!(
        runtime.update(|model| {
            assert!(model.start(0, 10));
            for n in 1..=1600 {
                let f = feature(n);
                model.observe(f, f.newest_us);
            }
            assert!(model.validate(16_000_000));
            for n in 1601..=3200 {
                let f = feature(n);
                model.observe(f, f.newest_us);
            }
            assert!(model.accept(32_000_000));
            let f = feature(3201);
            model.observe(f, f.newest_us);
        }),
        Some(())
    );
    let result = runtime.snapshot(32_010_000).unwrap();
    assert_eq!(result.phase, Phase::Ready);
    assert_eq!(result.state, State::NotDetected);
    runtime
}
fn main() {
    // No source callback or observation is dropped. Only a read-only poll sees
    // the model mutex while its writer holds it, as on the two ESP32-S3 cores.
    let runtime = ready();
    let locked = Barrier::new(2);
    let resume = Barrier::new(2);
    std::thread::scope(|scope| {
        let worker = scope.spawn(|| {
            runtime
                .update(|model| {
                    let f = feature(3202);
                    model.observe(f, f.newest_us);
                    locked.wait();
                    resume.wait();
                })
                .unwrap();
        });
        locked.wait();
        let poll = runtime.snapshot(32_020_000);
        resume.wait();
        worker.join().unwrap();
        assert!(poll.is_none());
    });
    let result = runtime.snapshot(32_020_000).unwrap();
    assert_eq!(result.phase, Phase::Rejected);
    assert_eq!(result.state, State::Unknown);
    println!("CALIBRATION_RUNTIME_WITNESS case=read_poll_contention source_observations_lost=0 result=rejected");
    // The clock samples themselves advance. A reader sampled now, was
    // preempted by fresh source publication, then used its older argument.
    let runtime = ready();
    let reader_sample = 32_010_000;
    let f = feature(3202);
    runtime.observe(f, f.newest_us);
    let result = runtime.snapshot(reader_sample).unwrap();
    assert_eq!(result.phase, Phase::Rejected);
    assert_eq!(result.state, State::Unknown);
    println!("CALIBRATION_RUNTIME_WITNESS case=reader_timestamp_before_publication clock_samples_monotonic=1 result=rejected");
}

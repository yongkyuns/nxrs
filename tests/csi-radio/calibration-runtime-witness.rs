//! Deterministic regression witnesses for the repaired experimental runtime.
//! Host scheduling/clock evidence only; no claim of physical RF qualification.
#![allow(dead_code)]
#[path = "presence/src/calibration.rs"]
mod calibration;
#[path = "presence/src/diagnostics.rs"]
mod diagnostics;
use calibration::{Config, Feature, Phase, ReadUnavailable, Rejection, Runtime, State};
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
    let result = runtime.snapshot(|| 32_010_000).unwrap();
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
        let poll = runtime.snapshot(|| 32_020_000);
        resume.wait();
        worker.join().unwrap();
        assert!(matches!(poll, Err(ReadUnavailable::Busy)));
    });
    let result = runtime.snapshot(|| 32_020_000).unwrap();
    assert_eq!(result.phase, Phase::Ready);
    assert_eq!(result.state, State::NotDetected);
    println!("CALIBRATION_RUNTIME_WITNESS case=read_poll_contention source_observations_lost=0 poll=unknown result=ready");
    // Construct the reader first, then preempt it before model access. Its
    // deferred clock must observe the later time after source publication.
    let runtime = ready();
    let clock = std::sync::atomic::AtomicU64::new(32_010_000);
    let reader_clock = || clock.load(std::sync::atomic::Ordering::Acquire);
    let f = feature(3202);
    clock.store(f.newest_us, std::sync::atomic::Ordering::Release);
    runtime.observe(f, || f.newest_us);
    let result = runtime.snapshot(reader_clock).unwrap();
    assert_eq!(result.phase, Phase::Ready);
    assert_eq!(result.state, State::NotDetected);
    println!("CALIBRATION_RUNTIME_WITNESS case=reader_preempted_before_publication clock_sampled_inside_access=1 result=ready");

    // Opposite ordering: a source update really is lost while a poll owns the
    // model. It must remain nonblocking and invalidate the next available read.
    let runtime = ready();
    let locked = Barrier::new(2);
    let resume = Barrier::new(2);
    std::thread::scope(|scope| {
        let reader = scope.spawn(|| {
            runtime
                .snapshot(|| {
                    locked.wait();
                    resume.wait();
                    32_010_000
                })
                .unwrap()
        });
        locked.wait();
        runtime.observe(feature(3202), || panic!("lost callback cannot sample time"));
        resume.wait();
        let poll = reader.join().unwrap();
        assert_eq!(poll.state, State::Unknown);
        assert_eq!(poll.rejection, Rejection::SourceContention);
    });
    let result = runtime.snapshot(|| 32_020_000).unwrap();
    assert_eq!(result.phase, Phase::Rejected);
    assert_eq!(result.rejection, Rejection::SourceContention);
    println!("CALIBRATION_RUNTIME_WITNESS case=lost_source_update result=rejected rejection=source_contention");
}

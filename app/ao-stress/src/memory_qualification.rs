//! Test-only probes: live event-buffer reuse and the real stress app lifecycle.
use super::stress::{self, Config, Scenario};
use event::{EventInbox, EventSender, bounded};
use nxrs_allocation_probe::{Tracking, begin_measurement};
use std::alloc::System;
use std::hint::black_box;
use std::sync::mpsc::RecvTimeoutError;
use std::thread;
use std::time::Duration;

#[global_allocator]
static ALLOCATOR: Tracking<System> = Tracking::new(System);

#[derive(Debug)]
struct Buffer {
    sequence: u64,
    bytes: Vec<u8>,
}

fn exchange(
    sender: &EventSender<Buffer>,
    inbox: &EventInbox<Buffer>,
    mut buffer: Buffer,
) -> Buffer {
    buffer.sequence += 1;
    buffer.bytes[0] = buffer.sequence as u8;
    sender.try_send(buffer).unwrap();
    let buffer = inbox.wait(Some(Duration::from_secs(2))).unwrap();
    assert_eq!(buffer.bytes[0], buffer.sequence as u8 ^ 0xa5);
    black_box(&buffer.bytes);
    buffer
}

fn caller_runtime_first_use() {
    // Rust 1.90 mpmc/context.rs caches an Arc<Inner> in the CALLING thread.
    // Joining an owner cannot destroy the still-live test caller's TLS. Measure
    // this explicitly instead of subtracting an arbitrary leak allowance.
    let cold = ALLOCATOR.snapshot();
    let mut samples = [cold; 2];
    for sample in &mut samples {
        let (sender, inbox) = bounded::<u8>(1);
        assert_eq!(
            inbox.wait(Some(Duration::from_millis(20))),
            Err(RecvTimeoutError::Timeout)
        );
        drop(sender);
        drop(inbox);
        *sample = ALLOCATOR.snapshot();
    }
    samples[1].reclaimed_since(samples[0]).unwrap();
    samples[0].report(cold, "ao-stress", "caller-runtime", "first-use");
    samples[1].report(samples[0], "ao-stress", "caller-runtime", "reuse");
}

fn live_owner_buffer_reuse() {
    let before = ALLOCATOR.snapshot();
    // Scope joins even during unwinding. Request ownership stays inside its
    // closure, so it is dropped before scope cleanup waits for a failed test.
    let (initialized, first, steady) = thread::scope(|scope| {
        let (sender, inbox) = bounded::<Buffer>(4);
        let (output, replies) = bounded(4);
        let owner = thread::Builder::new()
            .name("memory-owner".into())
            .stack_size(32768)
            .spawn_scoped(scope, move || {
                let mut handled = 0;
                loop {
                    let mut buffer = match inbox.wait(Some(Duration::from_secs(2))) {
                        Ok(buffer) => buffer,
                        Err(RecvTimeoutError::Disconnected) => break,
                        Err(RecvTimeoutError::Timeout) => panic!("memory owner timed out"),
                    };
                    handled += 1;
                    assert_eq!(buffer.sequence, handled);
                    assert_eq!(buffer.bytes.len(), 256);
                    buffer.bytes[0] ^= 0xa5;
                    if output.try_send(buffer).is_err() {
                        break;
                    }
                }
                handled
            })
            .unwrap();
        let mut buffer = Buffer {
            sequence: 0,
            bytes: vec![0u8; 256],
        };
        let initialized = ALLOCATOR.snapshot();
        // Separate owner/channel first use from the warmed window. Caller TLS
        // was measured independently; owner TLS must be reclaimed on join.
        for _ in 0..16 {
            buffer = exchange(&sender, &replies, buffer);
        }
        let first = ALLOCATOR.snapshot();
        for _ in 0..1024 {
            buffer = exchange(&sender, &replies, buffer);
        }
        let steady = ALLOCATOR.snapshot();
        drop(buffer);
        drop(sender);
        let handled = owner.join().unwrap();
        drop(replies);
        assert_eq!(handled, 1040);
        (initialized, first, steady)
    });
    let released = ALLOCATOR.snapshot();
    // Publish diagnostics after all sampling, including on a failing budget.
    initialized.report(before, "ao-stress", "owned-buffer", "setup");
    first.report(initialized, "ao-stress", "owned-buffer", "first-use");
    steady.report(first, "ao-stress", "owned-buffer", "steady");
    released.report(steady, "ao-stress", "owned-buffer", "teardown");
    released.report(before, "ao-stress", "owned-buffer", "reclaimed");
    steady.no_allocator_calls_since(first).unwrap();
    released.reclaimed_since(before).unwrap();
}

fn repeated_real_app_lifecycles() {
    for (scenario, name) in [
        (Scenario::Steady, "steady-lifecycle"),
        (Scenario::Burst, "burst-lifecycle"),
        (Scenario::SlowConsumer, "slow-consumer-lifecycle"),
        (Scenario::CpuLoad, "cpu-load-lifecycle"),
    ] {
        let config = Config {
            duration: Duration::from_millis(40),
            capacity: 1,
            ..Config::default()
        };
        let cold = ALLOCATOR.snapshot();
        let report = stress::run(&config, scenario).unwrap();
        black_box(&report);
        drop(report);
        let first = ALLOCATOR.snapshot();
        // First-use retention is reported, not confused with repeated growth.
        // One additional explicit warmup, never warm-until-the-test-passes.
        drop(stress::run(&config, scenario).unwrap());
        let baseline = ALLOCATOR.snapshot();
        for _ in 0..8 {
            let report = stress::run(&config, scenario).unwrap();
            black_box(&report);
            drop(report); // Include the returned report's owned Vec allocations.
            let reclaimed = ALLOCATOR.snapshot();
            assert!(
                reclaimed.reclaimed_since(baseline).is_ok(),
                "{name}: baseline={baseline:?}, reclaimed={reclaimed:?}"
            );
        }
        let end = ALLOCATOR.snapshot();
        first.report(cold, "ao-stress", name, "first-run");
        end.report(baseline, "ao-stress", name, "warmed-cycles");
    }
}

fn observer_negative_control() {
    let before = ALLOCATOR.snapshot();
    let buffer = black_box(vec![42u8; 512]);
    let held = ALLOCATOR.snapshot();
    assert!(held.no_allocator_calls_since(before).is_err());
    assert!(held.reclaimed_since(before).is_err());
    drop(buffer);
    ALLOCATOR.snapshot().reclaimed_since(before).unwrap();
    println!("MEMORY_CONTROL app=ao-stress kind=allocation-and-retention rejected=true");
}

pub(crate) fn run() {
    begin_measurement();
    observer_negative_control();
    caller_runtime_first_use();
    live_owner_buffer_reuse();
    repeated_real_app_lifecycles();
    println!("MEMORY_PASS app=ao-stress cases=6");
}

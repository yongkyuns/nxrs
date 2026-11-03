//! Matched, statically dispatched transport operations. Setup and validation
//! remain outside each named operation's timestamps.
use crate::{
    native::{self, Gate, Result, Schedule},
    payload::Payload,
    report::{Report, Sample},
    support::{self, Clock, Config, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS, WORKER_STACK},
    transport::{Admission, ReceiveEndpoint, SendEndpoint, Transport},
};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        Arc,
    },
    thread,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionCall {
    TrySend,
    Send,
}
impl AdmissionCall {
    pub fn name(self) -> &'static str {
        match self {
            Self::TrySend => "try-send",
            Self::Send => "send",
        }
    }
}

fn admit<P: Payload, S: SendEndpoint<P>>(sender: &S, value: P, call: AdmissionCall) -> Result<()> {
    match call {
        AdmissionCall::TrySend => sender.try_send(value),
        AdmissionCall::Send => sender.send(value),
    }
    .map_err(|error| match error {
        Admission::Full(_) => "unexpected-full",
        Admission::Closed(_) => "unexpected-closed",
    })
}

// Specialize the admission operation before entering the timed loop. The
// adapter and closure are monomorphized; there is no dynamic transport call.
pub fn ready<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    call: AdmissionCall,
    full: bool,
) -> Result<Report> {
    match call {
        AdmissionCall::TrySend => ready_with::<T, P>(config, clock, capacity, full, |tx, value| {
            tx.try_send(value)
        }),
        AdmissionCall::Send => {
            if full {
                return Err("full-rejection-requires-try-send");
            }
            ready_with::<T, P>(config, clock, capacity, false, |tx, value| tx.send(value))
        }
    }
}

fn ready_with<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    full: bool,
    send: impl Fn(&T::Tx<P>, P) -> std::result::Result<(), Admission<P>>,
) -> Result<Report> {
    let (tx, mut rx) = T::channel::<P>(capacity);
    let mut report = Report::new(config);
    report.sender = support::configure(SENDER_PRIORITY)?;
    report.receiver = report.sender;
    report.sender_tid = native::tid();
    report.receiver_tid = report.sender_tid;
    for sequence in 1..=config.total() {
        if full {
            for slot in 0..capacity {
                admit(&tx, P::new(slot as u32, 1), AdmissionCall::TrySend)?;
            }
        }
        let value = P::new(sequence, 0);
        let guard = native::monotonic_ns()?;
        let start = native::counter();
        let outcome = send(&tx, value);
        let admission_end = native::counter();
        let receive_start;
        let receive;
        if full {
            match outcome {
                Err(Admission::Full(returned)) if returned == value => (),
                _ => return Err("full-must-return-original-value"),
            }
            receive_start = None;
            receive = 0;
        } else {
            outcome.map_err(|_| "ready-admission")?;
            let before = native::counter();
            let received = rx.recv()?;
            receive = native::counter();
            receive_start = Some(before);
            if !received.valid(sequence, 0) {
                return Err("ready-payload");
            }
            report.checksum = report.checksum.wrapping_add(received.checksum());
            if sequence > config.warmup {
                report.retained_checksum =
                    report.retained_checksum.wrapping_add(received.checksum());
            }
        }
        let guard_end = native::monotonic_ns()?;
        let mut deltas = [admission_end.wrapping_sub(start), 0];
        if !full {
            deltas[1] = receive.wrapping_sub(start);
        }
        let window_ns = clock.window(guard, guard_end, &deltas)?;
        if full {
            // Prove rejection left the full queue and every payload intact.
            for slot in 0..capacity {
                let preserved = rx.recv()?;
                if !preserved.valid(slot as u32, 1) {
                    return Err("full-altered-queue");
                }
                report.checksum = report.checksum.wrapping_add(preserved.checksum());
                if sequence > config.warmup {
                    report.retained_checksum =
                        report.retained_checksum.wrapping_add(preserved.checksum());
                }
            }
        }
        if rx.try_recv()?.is_some() {
            return Err("ready-extra-message");
        }
        report.counts.attempted += 1;
        if full {
            report.counts.rejected += 1;
        } else {
            report.counts.admitted += 1;
            report.counts.handled += 1;
        }
        if sequence > config.warmup {
            report.samples.push(Sample {
                sequence,
                start,
                admission_end,
                receive_start,
                receive,
                window_ns,
                accepted: !full,
                ..Sample::default()
            });
        }
    }
    Ok(report)
}

struct Shared {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    announced: AtomicU32,
    completed: AtomicU32,
    receive: AtomicU32,
    valid: AtomicBool,
    cancelled: AtomicBool,
    terminal: AtomicBool,
}

pub fn blocked<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    call: AdmissionCall,
) -> Result<Report> {
    match call {
        AdmissionCall::TrySend => {
            blocked_with::<T, P>(config, clock, capacity, |tx, value| tx.try_send(value))
        }
        AdmissionCall::Send => {
            blocked_with::<T, P>(config, clock, capacity, |tx, value| tx.send(value))
        }
    }
}

fn blocked_with<T: Transport, P: Payload>(
    config: Config,
    clock: &Clock,
    capacity: usize,
    send: impl Fn(&T::Tx<P>, P) -> std::result::Result<(), Admission<P>>,
) -> Result<Report> {
    let (tx, mut rx) = T::channel::<P>(capacity);
    let mut report = Report::new(config);
    report.sender = support::configure(SENDER_PRIORITY)?;
    report.sender_tid = native::tid();
    let shared = Arc::new(Shared {
        tid: AtomicI32::new(-1),
        policy: AtomicI32::new(-1),
        priority: AtomicI32::new(-1),
        announced: AtomicU32::new(0),
        completed: AtomicU32::new(0),
        receive: AtomicU32::new(0),
        valid: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        terminal: AtomicBool::new(false),
    });
    let ready = Gate::new()?;
    let arm = Gate::new()?;
    let done = Gate::new()?;
    let terminal = Gate::new()?;
    let (worker, worker_ready, worker_arm, worker_done, worker_terminal) = (
        shared.clone(),
        ready.clone(),
        arm.clone(),
        done.clone(),
        terminal.clone(),
    );
    let handle = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let actual = support::configure(RECEIVER_PRIORITY);
            if let Ok(value) = actual {
                worker.policy.store(value.policy, Ordering::Relaxed);
                worker.priority.store(value.priority, Ordering::Relaxed);
                worker.tid.store(native::tid(), Ordering::Release);
            }
            worker_ready.post()?;
            actual?;
            for sequence in 1..=config.total() {
                worker_arm.wait(WAIT_MS)?;
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                // No other wait after announcement: on target the observer proves
                // this sequence is in the actual recv/selector's WAIT_SEM state.
                worker.announced.store(sequence, Ordering::Release);
                let received = rx.recv()?;
                let stamp = native::counter();
                worker.receive.store(stamp, Ordering::Relaxed);
                worker
                    .valid
                    .store(received.valid(sequence, 0), Ordering::Relaxed);
                worker.completed.store(sequence, Ordering::Release);
                worker_done.post()?;
            }
            worker.terminal.store(true, Ordering::Release);
            worker_terminal.wait(WAIT_MS)?;
            Ok(())
        })
        .map_err(|_| "receiver-spawn")?;
    let measured = (|| {
        ready.wait(WAIT_MS)?;
        report.receiver_tid = shared.tid.load(Ordering::Acquire);
        if report.receiver_tid < 1 || report.receiver_tid == report.sender_tid {
            return Err("receiver-scheduling");
        }
        report.receiver = Schedule {
            policy: shared.policy.load(Ordering::Relaxed),
            priority: shared.priority.load(Ordering::Relaxed),
        };
        for sequence in 1..=config.total() {
            arm.post()?;
            support::wait_until(|| shared.announced.load(Ordering::Acquire) == sequence)?;
            let proof = support::blocked(report.receiver_tid, report.receiver, || {
                shared.announced.load(Ordering::Acquire) == sequence
                    && shared.completed.load(Ordering::Acquire) < sequence
            })?;
            let value = P::new(sequence, 0);
            let guard = native::monotonic_ns()?;
            let start = native::counter();
            let admitted = send(&tx, value);
            let admission_end = native::counter();
            admitted.map_err(|_| "blocked-admission")?;
            done.wait(WAIT_MS)?;
            let guard_end = native::monotonic_ns()?;
            if shared.completed.load(Ordering::Acquire) != sequence
                || !shared.valid.load(Ordering::Relaxed)
            {
                return Err("blocked-sequence-or-payload");
            }
            let receive = shared.receive.load(Ordering::Relaxed);
            let window_ns = clock.window(
                guard,
                guard_end,
                &[
                    admission_end.wrapping_sub(start),
                    receive.wrapping_sub(start),
                ],
            )?;
            report.counts.attempted += 1;
            report.counts.admitted += 1;
            report.counts.handled += 1;
            report.checksum = report.checksum.wrapping_add(value.checksum());
            if sequence > config.warmup {
                report.retained_checksum = report.retained_checksum.wrapping_add(value.checksum());
                report.samples.push(Sample {
                    sequence,
                    start,
                    admission_end,
                    receive,
                    window_ns,
                    blocked: proof,
                    ..Sample::default()
                });
            }
        }
        support::wait_until(|| shared.terminal.load(Ordering::Acquire))?;
        report.terminal_quiescence_verified =
            support::blocked(report.receiver_tid, report.receiver, || {
                shared.terminal.load(Ordering::Acquire)
            })?;
        Ok(())
    })();
    // Closing the measured queue wakes recv/select even if a partial run failed
    // before any admission. Both out-of-band wait sites are always released.
    shared.cancelled.store(true, Ordering::Release);
    drop(tx);
    let terminal_release = terminal.post();
    let arm_release = arm.post();
    let joined = support::join_bounded(handle);
    measured?;
    terminal_release?;
    arm_release?;
    joined?;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        payload::Bytes16,
        transport::{Messaging, RawStd},
    };
    #[test]
    fn last_sample_is_retained_and_calls_are_distinct() {
        let clock = Clock::new().unwrap();
        let config = Config::new(4, 2).unwrap();
        for call in [AdmissionCall::TrySend, AdmissionCall::Send] {
            let raw = ready::<RawStd, Bytes16>(config, &clock, 8, call, false).unwrap();
            let facade = ready::<Messaging, Bytes16>(config, &clock, 8, call, false).unwrap();
            assert_eq!(raw.checksum, facade.checksum);
            assert_eq!(raw.samples.last().unwrap().sequence, config.total());
            assert_eq!(raw.counts.handled, 6);
            assert_eq!(raw.samples.len(), 4);
        }
        assert!(ready::<RawStd, Bytes16>(config, &clock, 8, AdmissionCall::Send, true).is_err());
        let full =
            ready::<Messaging, Bytes16>(config, &clock, 8, AdmissionCall::TrySend, true).unwrap();
        assert_eq!(full.counts.rejected, 6);
        assert!(full.samples.iter().all(|s| !s.accepted));
    }
    #[test]
    fn failed_admission_and_corrupted_delivery_join_the_blocked_worker() {
        let clock = Clock::new().unwrap();
        let config = Config::new(3, 1).unwrap();
        for fail_at in [1, 3] {
            let outcome = blocked_with::<RawStd, Bytes16>(config, &clock, 8, |tx, value| {
                if value.sequence() == fail_at {
                    Err(Admission::Closed(value))
                } else {
                    SendEndpoint::try_send(tx, value)
                }
            });
            assert!(matches!(outcome, Err("blocked-admission")));
        }
        let corrupt = blocked_with::<crate::transport::CrossbeamSelectThree, Bytes16>(
            config,
            &clock,
            8,
            |tx, mut value| {
                value.0[2] ^= 1;
                SendEndpoint::try_send(tx, value)
            },
        );
        assert!(matches!(corrupt, Err("blocked-sequence-or-payload")));
    }
}

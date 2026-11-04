use crate::{
    native::{self, Gate, Result, Schedule, State},
    pacing::{Observation, Pacing, Schedule as ReleaseSchedule},
    Clock, Message, CAPACITY, RECEIVER_PRIORITY, SENDER_PRIORITY, WAIT_MS, WORKER_STACK,
};
use std::{
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU32, Ordering},
        mpsc::sync_channel,
        Arc,
    },
    thread,
};

struct Shared {
    tid: AtomicI32,
    policy: AtomicI32,
    priority: AtomicI32,
    announced: AtomicU32,
    completed: AtomicU32,
    receive_ticks: AtomicU32,
    sequence_valid: AtomicBool,
    cancelled: AtomicBool,
    terminal_gate_ready: AtomicBool,
}

struct Sample {
    index: u32,
    send_start: u32,
    send_return: u32,
    receive_return: u32,
    elapsed_upper_ns: u64,
    blocked_verified: bool,
    pacing: Observation,
}

pub struct Report {
    pub sender: Schedule,
    pub receiver: Schedule,
    pub observer: Schedule,
    samples: Vec<Sample>,
    pub terminal_quiescence_verified: bool,
    release_profile: &'static str,
    release_seed: u32,
    fixed_delay_us: u32,
    retained_window_ns: u64,
    counter_read_min_ticks: u32,
    counter_read_max_ticks: u32,
}

pub fn run(
    samples: u32,
    warmup: u32,
    schedule: ReleaseSchedule,
    clock: &Clock,
) -> Result<Report> {
    let mut pacing = Pacing::new(schedule, samples, warmup)?;
    let mut counter_read_min_ticks = u32::MAX;
    let mut counter_read_max_ticks = 0;
    // Separate observation, never subtracted from sample latencies.
    for _ in 0..64 {
        let before = native::counter();
        let delta = native::counter().wrapping_sub(before);
        if delta >= (1 << 31) {
            return Err("counter-probe-reversed");
        }
        counter_read_min_ticks = counter_read_min_ticks.min(delta);
        counter_read_max_ticks = counter_read_max_ticks.max(delta);
    }
    let sender = if native::is_nuttx() {
        native::fifo(native::tid(), SENDER_PRIORITY)?
    } else {
        native::schedule(native::tid())?
    };
    let shared = Arc::new(Shared {
        tid: AtomicI32::new(-1),
        policy: AtomicI32::new(-1),
        priority: AtomicI32::new(-1),
        announced: AtomicU32::new(0),
        completed: AtomicU32::new(0),
        receive_ticks: AtomicU32::new(0),
        sequence_valid: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        terminal_gate_ready: AtomicBool::new(false),
    });
    let ready = Gate::new()?;
    let arm = Gate::new()?;
    let done = Gate::new()?;
    let terminal = Gate::new()?;
    let (tx, rx) = sync_channel::<Message>(CAPACITY);
    let mut measured = Vec::with_capacity(samples as usize);
    let (worker, ready_worker, arm_worker, done_worker, terminal_worker) = (
        shared.clone(),
        ready.clone(),
        arm.clone(),
        done.clone(),
        terminal.clone(),
    );
    let total = samples + warmup;
    let handle = thread::Builder::new()
        .stack_size(WORKER_STACK)
        .spawn(move || {
            let tid = native::tid();
            let actual = if native::is_nuttx() {
                native::fifo(tid, RECEIVER_PRIORITY)
            } else {
                native::schedule(tid)
            };
            if let Ok(value) = actual {
                worker.policy.store(value.policy, Ordering::Relaxed);
                worker.priority.store(value.priority, Ordering::Relaxed);
                worker.tid.store(tid, Ordering::Release);
            }
            ready_worker.post()?;
            actual?;
            for sequence in 1..=total {
                arm_worker.wait(WAIT_MS)?;
                if worker.cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                // No gate, sleep, logging, allocation or other explicit wait between
                // this announcement and recv. A fresh TCB WAIT_SEM observation is
                // required on NuttX before EVERY measured and warmup send.
                worker.announced.store(sequence, Ordering::Release);
                let message = rx.recv().map_err(|_| "receiver-disconnected")?;
                let stamp = native::counter();
                worker.receive_ticks.store(stamp, Ordering::Relaxed);
                worker
                    .sequence_valid
                    .store(message.valid(sequence), Ordering::Relaxed);
                worker.completed.store(sequence, Ordering::Release);
                done_worker.post()?;
            }
            // Hold the final receiver behind a gate until after the sender
            // records its final return timestamp. Otherwise receiver teardown
            // can preempt and contaminate the final send-call measurement.
            worker.terminal_gate_ready.store(true, Ordering::Release);
            terminal_worker.wait(WAIT_MS)?;
            Ok(())
        })
        .map_err(|_| "receiver-spawn")?;

    let measurement = (|| {
        ready.wait(WAIT_MS)?;
        let tid = shared.tid.load(Ordering::Acquire);
        if tid < 1 {
            return Err("receiver-scheduling");
        }
        let receiver = Schedule {
            policy: shared.policy.load(Ordering::Relaxed),
            priority: shared.priority.load(Ordering::Relaxed),
        };
        let mut retained_begin = None;
        for sequence in 1..=total {
            if sequence == warmup + 1 {
                retained_begin = Some(native::monotonic_ns()?);
            }
            // Setup-only pacing precedes the per-sample blocked-state proof.
            let pacing_sample = pacing.wait(clock)?;
            arm.post()?;
            crate::wait_until(|| shared.announced.load(Ordering::Acquire) == sequence)?;
            let blocked_verified = if native::is_nuttx() {
                crate::wait_until(|| {
                    let (state, priority) = native::state(tid);
                    state == State::WaitSem
                        && priority == receiver.priority
                        && shared.announced.load(Ordering::Acquire) == sequence
                        && shared.completed.load(Ordering::Acquire) < sequence
                })?;
                true
            } else {
                false
            };
            let message = Message::new(sequence);
            let window_start = native::monotonic_ns()?;
            let send_start = native::counter();
            tx.send(message).map_err(|_| "send-disconnected")?;
            let send_return = native::counter();
            done.wait(WAIT_MS)?;
            let window_end = native::monotonic_ns()?;
            if shared.completed.load(Ordering::Acquire) != sequence
                || !shared.sequence_valid.load(Ordering::Relaxed)
            {
                return Err("sequence-or-payload");
            }
            let receive_return = shared.receive_ticks.load(Ordering::Relaxed);
            // Include two declared monotonic resolution units conservatively.
            // The watchdog and native gate also bound failures independently.
            let elapsed_upper_ns = window_end
                .checked_sub(window_start)
                .ok_or("monotonic-reversed")?
                .checked_add(clock.monotonic_resolution_ns.saturating_mul(2))
                .ok_or("interval-overflow")?;
            if elapsed_upper_ns >= clock.wrap_upper_ns()
                || send_return.wrapping_sub(send_start) >= (1 << 31)
                || receive_return.wrapping_sub(send_start) >= (1 << 31)
            {
                return Err("counter-wrap-ambiguous");
            }
            if sequence > warmup {
                measured.push(Sample {
                    index: sequence - warmup - 1,
                    send_start,
                    send_return,
                    receive_return,
                    elapsed_upper_ns,
                    blocked_verified,
                    pacing: pacing_sample,
                });
            }
        }
        let retained_window_ns = native::monotonic_ns()?
            .checked_sub(retained_begin.ok_or("missing-retained-window")?)
            .ok_or("monotonic-reversed")?;
        crate::wait_until(|| shared.terminal_gate_ready.load(Ordering::Acquire))?;
        // Host cannot observe the native scheduler's WaitSem state.
        let terminal_quiescence_verified = if native::is_nuttx() {
            crate::wait_until(|| {
                let (state, priority) = native::state(tid);
                state == State::WaitSem
                    && priority == receiver.priority
                    && shared.terminal_gate_ready.load(Ordering::Acquire)
            })?;
            true
        } else {
            false
        };
        Ok((receiver, terminal_quiescence_verified, retained_window_ns))
    })();

    // Also clean up a partial run: close recv's channel and release a worker
    // that is still at the per-iteration gate. No detached live owner on error.
    shared.cancelled.store(true, Ordering::Release);
    drop(tx);
    // Release both wait sites, even on partial errors. Do not join the worker
    // while the terminal quiescence gate is still closed.
    let terminal_released = terminal.post();
    let released = arm.post();
    let joined = crate::join_bounded(handle);
    let (receiver, terminal_quiescence_verified, retained_window_ns) = measurement?;
    terminal_released?;
    released?;
    joined?;
    Ok(Report {
        sender,
        receiver,
        observer: sender,
        samples: measured,
        terminal_quiescence_verified,
        release_profile: pacing.profile(),
        release_seed: pacing.seed(),
        fixed_delay_us: pacing.fixed_delay_us(),
        retained_window_ns,
        counter_read_min_ticks,
        counter_read_max_ticks,
    })
}

impl Report {
    pub fn print(self, run_id: &str, clock: &Clock) -> Result<()> {
        let mut verified = 0;
        let mut nonzero = true;
        for sample in &self.samples {
            verified += u32::from(sample.blocked_verified);
            nonzero &= sample.receive_return.wrapping_sub(sample.send_start) > 0
                && sample.send_return.wrapping_sub(sample.send_start) > 0;
            println!(concat!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"sample\",",
                "\"run_id\":\"{}\",\"index\":{},\"send_start_ticks\":{},\"send_return_ticks\":{},\"receive_return_ticks\":{},",
                "\"elapsed_upper_ns\":{},\"blocked_verified\":{},\"sequence_valid\":true,",
                "\"pacing_requested_ticks\":{},\"pacing_start_ticks\":{},\"pacing_return_ticks\":{},\"pacing_elapsed_upper_ns\":{}}}"),
                run_id, sample.index, sample.send_start, sample.send_return, sample.receive_return, sample.elapsed_upper_ns, sample.blocked_verified,
                sample.pacing.requested_ticks, sample.pacing.start_ticks, sample.pacing.return_ticks,
                sample.pacing.elapsed_upper_ns);
        }
        println!(concat!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"result\",",
            "\"run_id\":\"{}\",\"success\":true,\"count\":{},\"blocked_verified_count\":{},",
            "\"threads_joined\":true,\"deadline_hit\":false,\"counter_resolved\":{},",
            "\"timing_qualified\":false,\"priority_progress_qualified\":false,",
            "\"terminal_quiescence_verified\":{},",
            "\"release_profile\":\"{}\",\"release_seed\":{},\"retained_window_ns\":{},",
            "\"release_fixed_delay_us\":{},",
            "\"counter_read_probe_count\":64,\"counter_read_min_ticks\":{},\"counter_read_max_ticks\":{},",
            "\"measurement_boundary\":\"send-start-to-recv-return\",\"send_duration_includes_receiver_preemption\":{}}}"),
            run_id, self.samples.len(), verified, nonzero && clock.counter_validated,
            self.terminal_quiescence_verified, self.release_profile, self.release_seed, self.retained_window_ns,
            self.fixed_delay_us, self.counter_read_min_ticks, self.counter_read_max_ticks, native::is_nuttx());
        Ok(())
    }
}

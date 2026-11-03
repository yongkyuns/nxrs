use crate::{
    native::{self, Gate, Result, Schedule, State},
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
}

struct Sample {
    index: u32,
    send_start: u32,
    send_return: u32,
    receive_return: u32,
    elapsed_upper_ns: u64,
    blocked_verified: bool,
}

pub struct Report {
    pub sender: Schedule,
    pub receiver: Schedule,
    pub observer: Schedule,
    samples: Vec<Sample>,
}

pub fn run(samples: u32, warmup: u32, clock: &Clock) -> Result<Report> {
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
    });
    let ready = Gate::new()?;
    let arm = Gate::new()?;
    let done = Gate::new()?;
    let (tx, rx) = sync_channel::<Message>(CAPACITY);
    let mut measured = Vec::with_capacity(samples as usize);
    let (worker, ready_worker, arm_worker, done_worker) =
        (shared.clone(), ready.clone(), arm.clone(), done.clone());
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
        for sequence in 1..=total {
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
                });
            }
        }
        Ok(receiver)
    })();

    // Also clean up a partial run: close recv's channel and release a worker
    // that is still at the per-iteration gate. No detached live owner on error.
    shared.cancelled.store(true, Ordering::Release);
    drop(tx);
    let released = arm.post();
    let joined = crate::join_bounded(handle);
    let receiver = measurement?;
    released?;
    joined?;
    Ok(Report {
        sender,
        receiver,
        observer: sender,
        samples: measured,
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
                "\"elapsed_upper_ns\":{},\"blocked_verified\":{},\"sequence_valid\":true}}"),
                run_id, sample.index, sample.send_start, sample.send_return, sample.receive_return, sample.elapsed_upper_ns, sample.blocked_verified);
        }
        println!(concat!("STD_CHANNEL_PERF {{\"schema\":\"std-channel-perf-v1\",\"record\":\"result\",",
            "\"run_id\":\"{}\",\"success\":true,\"count\":{},\"blocked_verified_count\":{},",
            "\"threads_joined\":true,\"deadline_hit\":false,\"counter_resolved\":{},",
            "\"timing_qualified\":false,\"priority_progress_qualified\":false,",
            "\"measurement_boundary\":\"send-start-to-recv-return\",\"send_duration_includes_receiver_preemption\":{}}}"),
            run_id, self.samples.len(), verified, nonzero && clock.counter_validated, native::is_nuttx());
        Ok(())
    }
}

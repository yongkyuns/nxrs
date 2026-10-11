// SPDX-License-Identifier: MIT
//! Matched implementation experiments, NOT Thread-Metric replacements.
use event::{bounded, EventInbox, EventSender};
use std::{ffi::c_void, sync::mpsc, thread};
type Message = [u32; 4];
const CAPACITY: usize = 8;
const STACK: usize = 32 * 1024;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct HeapSnapshot {
    supported: i32,
    arena: i32,
    used: i32,
    free_bytes: i32,
    peak: i32,
    largest_free: i32,
    allocated_chunks: i32,
}
unsafe extern "C" {
    fn rb_open(kind: u32, capacity: u32) -> *mut c_void;
    fn rb_step(object: *mut c_void, sequence: u32) -> i32;
    fn rb_close(object: *mut c_void) -> i32;
    fn rb_now_ns() -> u64;
    fn rb_resolution_ns() -> u64;
    fn rb_policy() -> i32;
    fn rb_priority() -> i32;
    fn rb_heap_snapshot(out: *mut HeapSnapshot) -> i32;
    fn rb_c_run(kind: u32, count: u32, capacity: u32) -> i32;
}
// Scalar-only ABI; time and all opaque OS objects remain in target-compiled C.
fn now() -> u64 {
    unsafe { rb_now_ns() }
}
fn msg(i: u32) -> Message {
    [i, !i, 0x12345678, 0x87654321]
}
fn context() -> (u64, i32, i32) {
    unsafe { (rb_resolution_ns(), rb_policy(), rb_priority()) }
}
fn heap() -> HeapSnapshot {
    let mut out = HeapSnapshot::default();
    let rc = unsafe { rb_heap_snapshot(&mut out) };
    assert_eq!(rc, 0, "heap snapshot failed");
    out
}
fn heap_extra(
    before: HeapSnapshot,
    setup: HeapSnapshot,
    active: HeapSnapshot,
    after: HeapSnapshot,
) -> String {
    format!(
        "\"heap_supported\":{},\"heap_used_before\":{},\"heap_used_setup\":{},\"heap_used_active\":{},\"heap_used_after\":{},\"heap_peak_before\":{},\"heap_peak_after\":{},\"heap_largest_free_before\":{},\"heap_largest_free_after\":{}",
        before.supported != 0,
        before.used,
        setup.used,
        active.used,
        after.used,
        before.peak,
        after.peak,
        before.largest_free,
        after.largest_free
    )
}
fn report(backend: &str, case: &str, n: u32, elapsed: u64, extra: &str) -> Result<(), String> {
    if elapsed == 0 {
        return Err("clock did not advance".into());
    }
    let (resolution, policy, priority) = context();
    println!("RTBENCH {{\"schema\":1,\"backend\":\"{backend}\",\"case\":\"{case}\",\"iterations\":{n},\"capacity\":{CAPACITY},\"elapsed_ns\":{elapsed},\"clock_resolution_ns\":{resolution},\"policy\":{policy},\"priority\":{priority},\"valid\":true,{extra}}}");
    Ok(())
}
fn posix(kind: u32, case: &str, n: u32) -> Result<(), String> {
    let heap_before = heap();
    // SAFETY: open returns an owned opaque C object, used serially until close.
    let object = unsafe { rb_open(kind, CAPACITY as u32) };
    if object.is_null() {
        return Err("POSIX setup failed".into());
    }
    let heap_setup = heap();
    let result: Result<_, String> = (|| {
        for i in 0..100 {
            if unsafe { rb_step(object, i) } != 0 {
                return Err("warmup failed".into());
            }
        }
        let start = now();
        for i in 0..n {
            if unsafe { rb_step(object, i) } != 0 {
                return Err("POSIX operation failed".into());
            }
        }
        Ok(now() - start)
    })();
    let heap_active = heap();
    let closed = unsafe { rb_close(object) };
    let heap_after = heap();
    let elapsed = result?;
    if closed != 0 {
        return Err("POSIX cleanup failed".into());
    }
    let extra = format!(
        "\"instrumentation\":\"interval-only\",{}",
        heap_extra(heap_before, heap_setup, heap_active, heap_after)
    );
    report("rust-posix", case, n, elapsed, &extra)
}
trait Sender: Send + 'static {
    fn put(&self, m: Message) -> Result<(), String>;
    fn try_put(&self, m: Message) -> Result<(), String>;
}
trait Inbox: Send + 'static {
    fn take(&self) -> Result<Message, String>;
    fn try_take(&self) -> Result<Message, String>;
}
impl Sender for mpsc::SyncSender<Message> {
    fn put(&self, m: Message) -> Result<(), String> {
        self.send(m).map_err(|e| e.to_string())
    }
    fn try_put(&self, m: Message) -> Result<(), String> {
        self.try_send(m).map_err(|e| e.to_string())
    }
}
impl Inbox for mpsc::Receiver<Message> {
    fn take(&self) -> Result<Message, String> {
        self.recv().map_err(|e| e.to_string())
    }
    fn try_take(&self) -> Result<Message, String> {
        self.try_recv().map_err(|e| e.to_string())
    }
}
impl Sender for EventSender<Message> {
    fn put(&self, m: Message) -> Result<(), String> {
        self.send(m).map_err(|e| e.to_string())
    }
    fn try_put(&self, m: Message) -> Result<(), String> {
        self.try_send(m).map_err(|e| e.to_string())
    }
}
impl Inbox for EventInbox<Message> {
    fn take(&self) -> Result<Message, String> {
        self.wait(None).map_err(|e| e.to_string())
    }
    fn try_take(&self) -> Result<Message, String> {
        self.try_recv().map_err(|e| e.to_string())
    }
}
trait Transport {
    type Tx: Sender;
    type Rx: Inbox;
    const NAME: &'static str;
    fn channel() -> (Self::Tx, Self::Rx);
}
struct Raw;
struct Ao;
impl Transport for Raw {
    type Tx = mpsc::SyncSender<Message>;
    type Rx = mpsc::Receiver<Message>;
    const NAME: &'static str = "rust-std";
    fn channel() -> (Self::Tx, Self::Rx) {
        mpsc::sync_channel(CAPACITY)
    }
}
impl Transport for Ao {
    type Tx = EventSender<Message>;
    type Rx = EventInbox<Message>;
    const NAME: &'static str = "rust-ao";
    fn channel() -> (Self::Tx, Self::Rx) {
        bounded(CAPACITY)
    }
}
fn hot<T: Transport>(n: u32) -> Result<(), String> {
    let heap_before = heap();
    let (tx, rx) = T::channel();
    let heap_setup = heap();
    let step = |i| -> Result<(), String> {
        let expected = msg(i);
        tx.try_put(expected)?;
        if rx.try_take()? != expected {
            return Err("payload mismatch".into());
        }
        Ok(())
    };
    for i in 0..100 {
        step(i)?;
    }
    let start = now();
    for i in 0..n {
        step(i)?;
    }
    let elapsed = now() - start;
    let heap_active = heap();
    drop((tx, rx));
    let heap_after = heap();
    let extra = format!(
        "\"instrumentation\":\"interval-only\",{}",
        heap_extra(heap_before, heap_setup, heap_active, heap_after)
    );
    report(T::NAME, "queue-hot", n, elapsed, &extra)
}
#[derive(Clone)]
struct Histogram {
    bins: [u32; 64],
    samples: u32,
    max: u64,
}
impl Histogram {
    fn new() -> Self {
        Self {
            bins: [0; 64],
            samples: 0,
            max: 0,
        }
    }
    fn add(&mut self, ns: u64) {
        // Bucket 0 covers 0..1 ns; bucket k is an inclusive power-of-two bound.
        let k = if ns <= 1 {
            0
        } else {
            (64 - (ns - 1).leading_zeros()) as usize
        };
        self.bins[k.min(63)] += 1;
        self.samples += 1;
        self.max = self.max.max(ns);
    }
    fn percentile(&self, percent: u32) -> u64 {
        let rank = (u64::from(self.samples) * u64::from(percent)).div_ceil(100);
        let mut seen = 0u64;
        for (k, count) in self.bins.iter().enumerate() {
            seen += u64::from(*count);
            if seen >= rank {
                return if k == 63 { u64::MAX } else { 1u64 << k };
            }
        }
        0
    }
}
fn ping_pong<T: Transport>(n: u32) -> Result<(), String> {
    let heap_before = heap();
    let (tx, input) = T::channel();
    let (output, rx) = T::channel();
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let worker = thread::Builder::new()
        .stack_size(STACK)
        .spawn(move || {
            ready_tx.send(context()).map_err(|e| e.to_string())?;
            let mut handled = 0u32;
            while let Ok(m) = input.take() {
                output.put(m)?; // Same echo handler and blocking policy in both variants.
                handled += 1;
            }
            Ok::<u32, String>(handled)
        })
        .map_err(|e| e.to_string())?;
    // Readiness is NOT proof the receive operation has parked in the OS.
    let result: Result<_, String> = (|| {
        let worker_context = ready_rx.recv().map_err(|e| e.to_string())?;
        let heap_setup = heap();
        for i in 0..100 {
            tx.put(msg(i))?;
            if rx.take()? != msg(i) {
                return Err("warmup payload mismatch".into());
            }
        }
        let mut histogram = Histogram::new();
        let start = now();
        for i in 0..n {
            let t = now();
            tx.put(msg(i))?;
            if rx.take()? != msg(i) {
                return Err("payload mismatch".into());
            }
            histogram.add(now() - t);
        }
        Ok((now() - start, histogram, worker_context, heap_setup, heap()))
    })();
    // Disconnect both directions even on error before joining. No detached owners.
    drop(tx);
    drop(rx);
    let joined = worker.join().map_err(|_| "worker panicked".to_owned());
    let (elapsed, histogram, (_, worker_policy, worker_priority), heap_setup, heap_active) =
        result?;
    if joined?? != n + 100 {
        return Err("worker count mismatch".into());
    }
    let heap_after = heap();
    let extra = format!(
        "\"instrumentation\":\"round-trip-sampled\",\"receiver_parked_verified\":false,\"worker_stack_requested\":{STACK},\"worker_policy\":{worker_policy},\"worker_priority\":{worker_priority},\"p50_upper_ns\":{},\"p99_upper_ns\":{},\"max_ns\":{},{}",
        histogram.percentile(50),
        histogram.percentile(99),
        histogram.max,
        heap_extra(heap_before, heap_setup, heap_active, heap_after)
    );
    report(T::NAME, "ping-pong", n, elapsed, &extra)
}
fn run() -> Result<(), String> {
    let a: Vec<_> = std::env::args().skip(1).collect();
    if a.len() != 3 {
        return Err("usage: rt-bench BACKEND CASE ITERATIONS".into());
    }
    let n: u32 = a[2].parse().map_err(|_| "invalid iterations")?;
    if n == 0 || n > 10_000_000 {
        return Err("iterations must be 1..10000000".into());
    }
    match (a[0].as_str(), a[1].as_str()) {
        ("rust-std", "queue-hot") => hot::<Raw>(n),
        ("rust-ao", "queue-hot") => hot::<Ao>(n),
        ("rust-std", "ping-pong") => ping_pong::<Raw>(n),
        ("rust-ao", "ping-pong") => ping_pong::<Ao>(n),
        (backend @ ("c-posix" | "rust-posix"), case) => {
            let kind = [
                "semaphore-hot",
                "heap128",
                "queue-hot",
                "yield-alone",
                "semaphore-handoff",
            ]
            .iter()
            .position(|s| *s == case)
            .ok_or("unsupported POSIX case")? as u32;
            if backend == "c-posix" {
                if unsafe { rb_c_run(kind, n, CAPACITY as u32) } != 0 {
                    return Err("C loop failed".into());
                }
                Ok(())
            } else {
                posix(kind, case, n)
            }
        }
        _ => Err("unsupported backend/case (no silent substitution)".into()),
    }
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("RTBENCH FAIL {e}");
            std::process::ExitCode::FAILURE
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn histogram_bounds() {
        for sample in [0, 1, 2, 3, 1023, 1024, 1025, u64::MAX] {
            let mut h = Histogram::new();
            h.add(sample);
            assert!(h.percentile(99) >= sample);
        }
    }
    #[test]
    fn queue_implementations() {
        hot::<Raw>(100).unwrap();
        hot::<Ao>(100).unwrap();
    }
    #[test]
    fn owner_join_accounting() {
        ping_pong::<Raw>(100).unwrap();
        ping_pong::<Ao>(100).unwrap();
    }
}

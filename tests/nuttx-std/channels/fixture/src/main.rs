#![feature(mpmc_channel)]
#[cfg(not(target_os = "nuttx"))]
compile_error!("this fixture requires a native NuttX build");

#[path = "../../src/controls.rs"]
mod controls;
use std::{
    sync::{
        atomic::{AtomicUsize, Ordering::SeqCst},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

unsafe extern "C" {
    fn std_compare_tid() -> i32;
    fn std_compare_fifo() -> i32;
    fn std_compare_schedule(tid: i32, values: *mut i32) -> i32;
    fn std_compare_set_schedule(tid: i32, policy: i32, priority: i32) -> i32;
    fn std_compare_state(tid: i32, priority: i32, waiting: i32) -> i32;
    fn std_compare_gate(index: i32, operation: i32) -> i32;
    fn std_compare_watchdog(start: i32) -> i32;
}

const CANDIDATE: &str = env!("STD_COMPARISON_CANDIDATE");
const ID: &str = env!("STD_COMPARISON_ID");
fn tid() -> usize {
    unsafe { std_compare_tid() as usize }
}
fn schedule(tid: usize) -> [i32; 2] {
    let mut values = [0; 2];
    assert_eq!(
        unsafe { std_compare_schedule(tid as i32, values.as_mut_ptr()) },
        0
    );
    values
}
fn set_schedule(tid: usize, policy: i32, priority: i32) {
    assert_eq!(
        unsafe { std_compare_set_schedule(tid as i32, policy, priority) },
        0
    );
    assert_eq!(schedule(tid), [policy, priority]);
}
fn fifo(tid: usize, priority: i32) {
    set_schedule(tid, unsafe { std_compare_fifo() }, priority);
}
fn state(tid: usize, priority: i32, waiting: bool) -> bool {
    unsafe { std_compare_state(tid as i32, priority, i32::from(waiting)) == 1 }
}
fn gate(index: i32, operation: i32) {
    assert_eq!(unsafe { std_compare_gate(index, operation) }, 0);
}
fn until(mut observed: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(5);
    while !observed() {
        assert!(
            Instant::now() < end,
            "bounded scheduling observation expired"
        );
        thread::sleep(Duration::from_millis(1));
    }
}
fn spawn<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static) -> thread::JoinHandle<T> {
    thread::Builder::new()
        .stack_size(16384)
        .spawn(work)
        .unwrap()
}

static PRODUCER: AtomicUsize = AtomicUsize::new(0);
static RECEIVER: AtomicUsize = AtomicUsize::new(0);
static QUEUE: AtomicUsize = AtomicUsize::new(0);
static RESERVED: AtomicUsize = AtomicUsize::new(0);
static RETURNED: AtomicUsize = AtomicUsize::new(0);
static SPINS: AtomicUsize = AtomicUsize::new(0);
static SENT: AtomicUsize = AtomicUsize::new(0);
static RECEIVED: AtomicUsize = AtomicUsize::new(0);

// Only the upstream-queue experiment inserts these calls. The first is after
// tail reservation but before payload/stamp publication; waking the receiver
// forces a real priority preemption at that boundary. No sleeping is injected.
#[no_mangle]
pub extern "C" fn __nxrs_std_compare_reserved(queue: usize) {
    if PRODUCER.load(SeqCst) != tid() || RESERVED.load(SeqCst) != 0 {
        return;
    }
    QUEUE.store(queue, SeqCst);
    RESERVED.store(1, SeqCst);
    gate(0, 2);
    RETURNED.store(1, SeqCst);
}
#[no_mangle]
pub extern "C" fn __nxrs_std_compare_unpublished(queue: usize) {
    if RECEIVER.load(SeqCst) == tid() && QUEUE.load(SeqCst) == queue {
        SPINS.fetch_add(1, SeqCst);
    }
}

fn reservation() {
    assert_eq!(
        CANDIDATE, "upstream-queue",
        "only the instrumented candidate supplies this witness"
    );
    let before = schedule(0);
    fifo(0, 220);
    let (tx, rx) = mpsc::sync_channel(1);
    let receiver = spawn(move || {
        fifo(0, 180);
        RECEIVER.store(tid(), SeqCst);
        gate(0, 1);
        assert_eq!(rx.try_recv().unwrap(), 42);
        RECEIVED.store(1, SeqCst);
        assert_eq!(schedule(0), [unsafe { std_compare_fifo() }, 180]);
    });
    let producer = spawn(move || {
        fifo(0, 120);
        PRODUCER.store(tid(), SeqCst);
        gate(1, 1);
        tx.send(42).unwrap();
        SENT.store(1, SeqCst);
        assert_eq!(schedule(0), [unsafe { std_compare_fifo() }, 200]);
    });
    until(|| state(PRODUCER.load(SeqCst), 120, true) && state(RECEIVER.load(SeqCst), 180, true));
    gate(1, 2);
    until(|| SPINS.load(SeqCst) > 1);
    let first = SPINS.load(SeqCst);
    thread::sleep(Duration::from_millis(20));
    let last = SPINS.load(SeqCst);
    assert!(last > first);
    assert_eq!(RESERVED.load(SeqCst), 1);
    assert_eq!(
        (
            RETURNED.load(SeqCst),
            SENT.load(SeqCst),
            RECEIVED.load(SeqCst)
        ),
        (0, 0, 0)
    );
    assert!(state(PRODUCER.load(SeqCst), 120, false));
    assert!(state(RECEIVER.load(SeqCst), 180, false));
    // Rescue is part of the oracle: both runnable tasks must finish without
    // changing queue state or bypassing the original send/try_recv operations.
    fifo(PRODUCER.load(SeqCst), 200);
    producer.join().unwrap();
    receiver.join().unwrap();
    assert_eq!(
        (
            RETURNED.load(SeqCst),
            SENT.load(SeqCst),
            RECEIVED.load(SeqCst)
        ),
        (1, 1, 1)
    );
    set_schedule(0, before[0], before[1]);
    println!("STD_COMPARISON {{\"schema\":1,\"candidate\":\"{CANDIDATE}\",\"id\":\"{ID}\",\"case\":\"reservation\",\"passed\":true,\"hazard_observed\":true,\"upstream_fixed_priority_safe\":false,\"spins_before\":{first},\"spins_after\":{last},\"producer_priority\":120,\"receiver_priority\":180,\"coordinator_priority\":220,\"rescue_priority\":200,\"joined\":2}}");
}

fn parking() {
    let participant = Arc::new(AtomicUsize::new(0));
    let phase = Arc::new(AtomicUsize::new(0));
    let payload = Arc::new(AtomicUsize::new(0));
    let (id, ready, value) = (participant.clone(), phase.clone(), payload.clone());
    let worker = spawn(move || {
        fifo(0, 100);
        id.store(tid(), SeqCst);
        gate(0, 1);
        thread::park(); // exercise notifications issued before the first park
        ready.store(1, SeqCst);
        while value.load(SeqCst) == 0 {
            thread::park();
        }
        assert_eq!(value.load(SeqCst), 42);
    });
    until(|| state(participant.load(SeqCst), 100, true));
    worker.thread().unpark();
    worker.thread().unpark();
    gate(0, 2);
    until(|| phase.load(SeqCst) == 1 && state(participant.load(SeqCst), 100, true));
    payload.store(42, SeqCst);
    worker.thread().unpark();
    worker.join().unwrap();
    println!("PASS pre-issued notifications, native parked wait, wake and joined retirement");
}

fn main() {
    let case = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "functional".into());
    if case != "functional" && case != "reservation" {
        println!("STD_COMPARISON FAIL unknown-case");
        std::process::exit(1);
    }
    assert_eq!(unsafe { std_compare_watchdog(1) }, 0);
    for index in 0..2 {
        gate(index, 0);
    }
    if case == "reservation" {
        reservation();
    } else {
        fifo(0, 100);
        controls::run();
        parking();
        println!("STD_COMPARISON {{\"schema\":1,\"candidate\":\"{CANDIDATE}\",\"id\":\"{ID}\",\"case\":\"functional\",\"passed\":true,\"controls\":9}}");
    }
    for index in 0..2 {
        gate(index, 3);
    }
    assert_eq!(unsafe { std_compare_watchdog(0) }, 0);
}

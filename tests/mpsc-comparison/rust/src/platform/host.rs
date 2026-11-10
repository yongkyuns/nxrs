pub const CLOCK_HZ: u32 = 240000000;
use std::{
    sync::{
        Condvar, Mutex, OnceLock,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
pub const NAME: &str = "host";
pub const NATIVE: bool = false;
pub type Schedule = [i32; 2];
fn epoch() -> &'static Instant {
    static V: OnceLock<Instant> = OnceLock::new();
    V.get_or_init(Instant::now)
}
pub fn clock() -> u32 {
    (epoch().elapsed().as_nanos() * 240 / 1000) as u32
}
pub fn monotonic_ns() -> u64 {
    epoch().elapsed().as_nanos() as u64
}
pub fn sleep_ns(ns: u64) {
    std::thread::sleep(Duration::from_nanos(ns));
}
pub fn schedule() -> Schedule {
    [0, 0]
}
pub fn restore(_: Schedule) {}
pub fn priority() -> u32 {
    0
}
pub fn configure() -> u32 {
    0
}
pub fn policy() -> u32 {
    0
}
pub fn tid() -> usize {
    static NEXT: AtomicUsize = AtomicUsize::new(1);
    thread_local! {static ID:usize=NEXT.fetch_add(1,Ordering::Relaxed);}
    ID.with(|v| *v)
}
pub fn waiting(_: usize) -> bool {
    true
} // No native scheduling claim on a host.
pub fn queue_waiting(_: usize, _: bool) -> bool {
    false
} // A native queue is unavailable on the host.
struct Gate {
    count: Mutex<u32>,
    changed: Condvar,
}
fn gates() -> &'static [Gate] {
    static V: OnceLock<Vec<Gate>> = OnceLock::new();
    V.get_or_init(|| {
        (0..20)
            .map(|_| Gate {
                count: Mutex::new(0),
                changed: Condvar::new(),
            })
            .collect()
    })
}
pub fn gate_init(i: usize) {
    assert_eq!(*gates()[i].count.lock().unwrap(), 0);
}
pub fn gate_post(i: usize) {
    let g = &gates()[i];
    *g.count.lock().unwrap() += 1;
    g.changed.notify_one();
}
pub fn gate_wait(i: usize) {
    let g = &gates()[i];
    let n = g.count.lock().unwrap();
    let (mut n, r) = g
        .changed
        .wait_timeout_while(n, Duration::from_secs(10), |n| *n == 0)
        .unwrap();
    assert!(!r.timed_out());
    *n -= 1;
}
pub fn gate_destroy(i: usize) {
    assert_eq!(*gates()[i].count.lock().unwrap(), 0);
}
pub fn runtime(_: usize) -> u64 {
    u64::MAX
}
pub fn stack(_: usize) -> [u32; 3] {
    [0; 3]
}
pub fn heap() -> [u32; 4] {
    [0; 4]
}
pub fn watchdog_start(_: u32) {}
pub fn watchdog_stop() {}
struct PulseState {
    deadline: Option<u32>,
    quit: bool,
}
struct Pulse {
    state: std::sync::Arc<(Mutex<PulseState>, Condvar)>,
    worker: std::thread::JoinHandle<()>,
}
static PULSE: Mutex<Option<Pulse>> = Mutex::new(None);
pub fn native_ticks() -> u64 {
    monotonic_ns() / 10_000_000
}
pub fn pulse_init(callback: extern "C" fn()) {
    let state = std::sync::Arc::new((
        Mutex::new(PulseState {
            deadline: None,
            quit: false,
        }),
        Condvar::new(),
    ));
    let copy = state.clone();
    let worker = std::thread::spawn(move || {
        let (lock, changed) = &*copy;
        let mut state = lock.lock().unwrap();
        while !state.quit {
            if let Some(tick) = state.deadline {
                let remaining = (u64::from(tick) * 10_000_000).saturating_sub(monotonic_ns());
                if remaining != 0 {
                    state = changed
                        .wait_timeout(state, Duration::from_nanos(remaining))
                        .unwrap()
                        .0;
                } else {
                    state.deadline = None;
                    drop(state);
                    callback();
                    state = lock.lock().unwrap();
                }
            } else {
                state = changed.wait(state).unwrap();
            }
        }
    });
    assert!(
        PULSE
            .lock()
            .unwrap()
            .replace(Pulse { state, worker })
            .is_none()
    );
}
pub fn irq_arm(ticks: u32) {
    let guard = PULSE.lock().unwrap();
    let p = guard.as_ref().unwrap();
    p.state.0.lock().unwrap().deadline = Some(ticks);
    p.state.1.notify_one();
}
pub fn irq_post(index: usize) {
    gate_post(index);
}
pub fn pulse_stop() {
    let p = PULSE.lock().unwrap().take().unwrap();
    p.state.0.lock().unwrap().quit = true;
    p.state.1.notify_one();
    p.worker.join().unwrap();
}
pub fn output(bytes: &[u8]) {
    use std::io::Write;
    std::io::stdout().write_all(bytes).unwrap();
}
pub fn fatal() -> ! {
    std::process::abort()
}
pub fn queue_create(_: usize, _: usize) {
    panic!("native queue unavailable on host");
}
pub fn queue_send<const W: usize>(_: &[u32; W], _: bool) -> bool {
    unreachable!()
}
pub fn queue_receive<const W: usize>(_: &mut [u32; W]) {
    unreachable!()
}
pub fn queue_destroy() {}
pub struct Threads {
    handles: Vec<std::thread::JoinHandle<()>>,
}
impl Threads {
    pub fn new() -> Self {
        Self {
            handles: Vec::new(),
        }
    }
    pub fn spawn(&mut self, entry: extern "C" fn(usize, usize), context: usize, role: usize) {
        self.handles
            .push(std::thread::spawn(move || entry(context, role)));
    }
    pub fn join(self) {
        for h in self.handles {
            h.join().unwrap();
        }
    }
}

pub fn main() {
    let args: Vec<_> = std::env::args().collect();
    let words: Vec<_> = args[1..].iter().map(String::as_str).collect();
    crate::execute_std(&words);
}

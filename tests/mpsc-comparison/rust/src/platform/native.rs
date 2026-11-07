include!(concat!(env!("OUT_DIR"), "/clock.rs"));
use crate::check;
pub const NATIVE: bool = true;
pub type Schedule = [i32; 2];
unsafe extern "C" {
    fn mp_clock_prepare();
    fn mp_native_ticks() -> u64;
    fn mp_pulse_init(callback: extern "C" fn()) -> i32;
    fn mp_pulse_arm(ticks: u32) -> i32;
    fn mp_pulse_stop() -> i32;
    fn mp_clock32() -> u32;
    fn mp_monotonic_ns() -> u64;
    fn mp_sleep_ns(ns: u64) -> i32;
    fn mp_get_schedule(values: *mut i32) -> i32;
    fn mp_set_schedule(policy: i32, priority: i32) -> i32;
    fn mp_tid() -> usize;
    fn mp_waiting(tid: usize) -> i32;
    fn mp_gate_init(index: usize) -> i32;
    fn mp_gate_post(index: usize) -> i32;
    fn mp_gate_wait(index: usize) -> i32;
    fn mp_gate_destroy(index: usize) -> i32;
    fn mp_queue_create(capacity: usize, bytes: usize) -> i32;
    fn mp_queue_send(data: *const u8, blocking: i32) -> i32;
    fn mp_queue_receive(data: *mut u8) -> i32;
    fn mp_queue_destroy() -> i32;
    fn mp_runtime(tid: usize) -> u64;
    fn mp_stack(tid: usize, values: *mut u32) -> i32;
    fn mp_heap(values: *mut u32);
    fn mp_watchdog_start(ms: u32, callback: extern "C" fn()) -> i32;
    fn mp_watchdog_stop() -> i32;
    fn mp_output(data: *const u8, bytes: usize) -> i32;
    fn mp_fatal() -> !;
}
pub fn native_ticks() -> u64 {
    unsafe { mp_native_ticks() }
}
pub fn pulse_init(callback: extern "C" fn()) {
    check(unsafe { mp_pulse_init(callback) } == 0, "pulse-init");
}
pub fn irq_post(index: usize) {
    if unsafe { mp_gate_post(index) } != 0 {
        fatal();
    }
}
pub fn irq_arm(ticks: u32) {
    if unsafe { mp_pulse_arm(ticks) } != 0 {
        fatal();
    }
}
pub fn pulse_stop() {
    let status = unsafe { mp_pulse_stop() };
    check(status == 0 || status == -22, "pulse-stop");
}
pub fn clock() -> u32 {
    unsafe { mp_clock32() }
}
pub fn monotonic_ns() -> u64 {
    unsafe { mp_monotonic_ns() }
}
pub fn sleep_ns(ns: u64) {
    check(unsafe { mp_sleep_ns(ns) } == 0, "sleep");
}
pub fn schedule() -> Schedule {
    let mut v = [0; 2];
    unsafe { mp_clock_prepare() };
    check(
        unsafe { mp_get_schedule(v.as_mut_ptr()) } == 0,
        "schedule-get",
    );
    v
}
pub fn restore(v: Schedule) {
    check(unsafe { mp_set_schedule(v[0], v[1]) } == 0, "schedule-set");
}
pub fn priority() -> u32 {
    let mut v = [0; 2];
    check(
        unsafe { mp_get_schedule(v.as_mut_ptr()) } == 0,
        "schedule-read",
    );
    v[1] as u32
}
pub fn tid() -> usize {
    unsafe { mp_tid() }
}
pub fn policy() -> u32 {
    let mut v = [0; 2];
    check(
        unsafe { mp_get_schedule(v.as_mut_ptr()) } == 0,
        "schedule-policy",
    );
    v[0] as u32
}
pub fn waiting(tid: usize) -> bool {
    unsafe { mp_waiting(tid) == 1 }
}
pub fn gate_init(i: usize) {
    check(unsafe { mp_gate_init(i) } == 0, "gate-init");
}
pub fn gate_post(i: usize) {
    check(unsafe { mp_gate_post(i) } == 0, "gate-post");
}
pub fn gate_wait(i: usize) {
    check(unsafe { mp_gate_wait(i) } == 0, "gate-wait");
}
pub fn gate_destroy(i: usize) {
    check(unsafe { mp_gate_destroy(i) } == 0, "gate-destroy");
}
pub fn queue_create(capacity: usize, bytes: usize) {
    check(
        unsafe { mp_queue_create(capacity, bytes) } == 0,
        "queue-create",
    );
}
pub fn queue_send<const W: usize>(p: &[u32; W], blocking: bool) -> bool {
    let status = unsafe { mp_queue_send(p.as_ptr().cast(), i32::from(blocking)) };
    check(matches!(status, 0 | 1), "queue-send");
    status == 0
}
pub fn queue_receive<const W: usize>(p: &mut [u32; W]) {
    check(
        unsafe { mp_queue_receive(p.as_mut_ptr().cast()) } == 0,
        "queue-receive",
    );
}
pub fn queue_destroy() {
    check(unsafe { mp_queue_destroy() } == 0, "queue-destroy");
}
pub fn runtime(tid: usize) -> u64 {
    unsafe { mp_runtime(tid) }
}
pub fn stack(tid: usize) -> [u32; 3] {
    let mut v = [0; 3];
    check(unsafe { mp_stack(tid, v.as_mut_ptr()) } == 0, "stack-read");
    v
}
pub fn heap() -> [u32; 4] {
    let mut v = [0; 4];
    unsafe { mp_heap(v.as_mut_ptr()) };
    v
}
pub fn watchdog_start(ms: u32) {
    check(
        unsafe { mp_watchdog_start(ms, crate::mpsc_deadline) } == 0,
        "watchdog-start",
    );
}
pub fn watchdog_stop() {
    check(unsafe { mp_watchdog_stop() } == 0, "watchdog-stop");
}
pub fn output(bytes: &[u8]) {
    if unsafe { mp_output(bytes.as_ptr(), bytes.len()) } != 0 {
        fatal();
    }
}
pub fn fatal() -> ! {
    unsafe { mp_fatal() }
}

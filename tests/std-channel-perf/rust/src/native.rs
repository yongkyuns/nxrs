//! Mechanical native witness boundary. The workload remains ordinary Rust std.
use std::{
    cell::UnsafeCell,
    ffi::c_void,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};

unsafe extern "C" {
    fn scp_has_tcb_witness() -> i32;
    fn scp_current_tid() -> i32;
    fn scp_schedule_get(tid: i32, policy: *mut i32, priority: *mut i32) -> i32;
    fn scp_schedule_set(tid: i32, policy: i32, priority: i32) -> i32;
    fn scp_thread_state(tid: i32, priority: *mut i32) -> i32;
    fn scp_gate_init(storage: *mut c_void) -> i32;
    pub fn scp_gate_post(storage: *mut c_void) -> i32;
    fn scp_gate_wait_ms(storage: *mut c_void, timeout_ms: u32) -> i32;
    fn scp_gate_destroy(storage: *mut c_void) -> i32;
    fn scp_monotonic_ns(value: *mut u64) -> i32;
    fn scp_monotonic_resolution_ns(value: *mut u64) -> i32;
    fn scp_counter_info(
        request: u32,
        frequency: *mut u64,
        source: *mut u32,
        native_bits: *mut u32,
    ) -> i32;
    fn scp_counter() -> u32;
}

pub type Result<T> = std::result::Result<T, &'static str>;

pub fn is_nuttx() -> bool {
    unsafe { scp_has_tcb_witness() == 1 }
}
pub fn tid() -> i32 {
    unsafe { scp_current_tid() }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Schedule {
    pub policy: i32,
    pub priority: i32,
}

pub fn schedule(tid: i32) -> Result<Schedule> {
    let mut value = Schedule::default();
    if unsafe { scp_schedule_get(tid, &mut value.policy, &mut value.priority) } != 0 {
        return Err("schedule-read");
    }
    Ok(value)
}

pub fn set_schedule(tid: i32, schedule: Schedule) -> Result<()> {
    if unsafe { scp_schedule_set(tid, schedule.policy, schedule.priority) } != 0 {
        return Err("schedule-set");
    }
    let actual = self::schedule(tid)?;
    if actual.policy != schedule.policy || actual.priority != schedule.priority {
        return Err("schedule-verification");
    }
    Ok(())
}

pub fn fifo(tid: i32, priority: i32) -> Result<Schedule> {
    let requested = Schedule {
        policy: 1,
        priority,
    };
    set_schedule(tid, requested)?;
    Ok(requested)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Unavailable,
    WaitSem,
    Ready,
    Running,
    Other,
    Absent,
}

impl State {
    pub fn label(self) -> &'static str {
        match self {
            Self::Unavailable => "unavailable",
            Self::WaitSem => "wait_sem",
            Self::Ready => "ready",
            Self::Running => "running",
            Self::Other => "other",
            Self::Absent => "absent",
        }
    }
}

pub fn state(tid: i32) -> (State, i32) {
    let mut priority = -1;
    let state = match unsafe { scp_thread_state(tid, &mut priority) } {
        0 => State::Unavailable,
        1 => State::WaitSem,
        2 => State::Ready,
        3 => State::Running,
        5 => State::Absent,
        _ => State::Other,
    };
    (state, priority)
}

#[repr(C, align(16))]
struct GateStorage([u8; 128]);

pub struct Gate {
    storage: UnsafeCell<GateStorage>,
    initialized: AtomicBool,
}
// sem_t is initialized at its final Arc address and all accesses use native APIs.
unsafe impl Send for Gate {}
unsafe impl Sync for Gate {}

impl Gate {
    pub fn new() -> Result<Arc<Self>> {
        let gate = Arc::new(Self {
            storage: UnsafeCell::new(GateStorage([0; 128])),
            initialized: AtomicBool::new(false),
        });
        if unsafe { scp_gate_init(gate.pointer()) } != 0 {
            return Err("gate-initialization");
        }
        gate.initialized.store(true, Ordering::Relaxed);
        Ok(gate)
    }
    pub fn pointer(&self) -> *mut c_void {
        self.storage.get().cast()
    }
    pub fn post(&self) -> Result<()> {
        if unsafe { scp_gate_post(self.pointer()) } == 0 {
            Ok(())
        } else {
            Err("gate-post")
        }
    }
    pub fn wait(&self, timeout_ms: u32) -> Result<()> {
        if unsafe { scp_gate_wait_ms(self.pointer(), timeout_ms) } == 0 {
            Ok(())
        } else {
            Err("gate-wait-deadline-or-error")
        }
    }
}

impl Drop for Gate {
    fn drop(&mut self) {
        if self.initialized.load(Ordering::Relaxed)
            && unsafe { scp_gate_destroy(self.pointer()) } != 0
        {
            // Returning success with a live native waiter is forbidden.
            crate::fatal("gate-destroy");
        }
    }
}

pub fn monotonic_ns() -> Result<u64> {
    let mut value = 0;
    if unsafe { scp_monotonic_ns(&mut value) } != 0 {
        return Err("monotonic-clock");
    }
    Ok(value)
}

pub fn resolution_ns() -> Result<u64> {
    let mut value = 0;
    if unsafe { scp_monotonic_resolution_ns(&mut value) } != 0 || value == 0 {
        return Err("monotonic-resolution");
    }
    Ok(value)
}

pub fn counter_info(request: u32) -> Result<(u64, u32, u32)> {
    let (mut hz, mut source, mut bits) = (0, 0, 0);
    if unsafe { scp_counter_info(request, &mut hz, &mut source, &mut bits) } != 0 {
        return Err("counter-initialization");
    }
    Ok((hz, source, bits))
}

#[inline]
pub fn counter() -> u32 {
    unsafe { scp_counter() }
}

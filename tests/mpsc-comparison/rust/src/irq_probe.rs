//! A separate, deliberately perturbing hardware-alarm response diagnostic.
//! Single-core target1 delivery is stopped and quiescent before reading state.
use crate::{
    check,
    irq_protocol::{CAPACITY, HZ, MASK, PERIOD, Phase},
    line,
};
use core::{
    cell::UnsafeCell,
    sync::atomic::{Ordering, compiler_fence},
};

unsafe extern "C" {
    fn mp_irq_probe_open(callback: extern "C" fn(u64), info: *mut u32) -> i32;
    fn mp_irq_probe_now() -> u64;
    fn mp_irq_probe_start(deadline: u64) -> i32;
    fn mp_irq_probe_arm(deadline: u64) -> i32;
    fn mp_irq_probe_stop(end: *mut u64, pending: *mut u32) -> i32;
    fn mp_irq_probe_close() -> i32;
}

struct Probe {
    phases: [Phase; 2],
    info: [u32; 7],
    active: usize,
    opened: bool,
    closed: bool,
}
struct Shared(UnsafeCell<Probe>);
// Only the fixed probe ISR accesses the active phase. Task code writes before
// enable and reads after the native single-core disable/quiescence boundary.
unsafe impl Sync for Shared {}
static PROBE: Shared = Shared(UnsafeCell::new(Probe {
    phases: [Phase::new(), Phase::new()],
    info: [0; 7],
    active: 2,
    opened: false,
    closed: false,
}));

#[unsafe(no_mangle)]
#[unsafe(link_section = ".iram1.mpsc_irq_entry")]
#[inline(never)]
pub extern "C" fn mpsc_irq_probe_entry(entered: u64) {
    // No allocation, output, panic or OS wait. Unexpected protocol/arm failures
    // stop rearming and are checked in task context after quiescence.
    unsafe {
        let probe = &mut *PROBE.0.get();
        if probe.active < 2 {
            let phase = probe.phases.get_unchecked_mut(probe.active);
            if let Some(next) = phase.entry(entered) {
                if mp_irq_probe_arm(next) != 0 {
                    phase.error = 5;
                }
            }
        }
    }
}

pub fn prepare() {
    unsafe {
        let p = &mut *PROBE.0.get();
        check(!p.opened && !p.closed, "irq-observer-fresh");
        check(
            mp_irq_probe_open(mpsc_irq_probe_entry, p.info.as_mut_ptr()) == 0,
            "irq-observer-resource-unavailable",
        );
        p.opened = true;
    }
}

fn start_phase(index: usize) {
    let now = unsafe { mp_irq_probe_now() };
    check(now <= MASK, "irq-counter-read");
    let next = unsafe {
        let p = &mut *PROBE.0.get();
        check(p.opened && !p.closed && p.active == 2, "irq-phase-state");
        p.active = index;
        p.phases[index].begin(now);
        p.phases[index].next
    };
    compiler_fence(Ordering::Release);
    let status = unsafe { mp_irq_probe_start(next) };
    if status != 0 {
        let _ = unsafe { mp_irq_probe_close() };
        check(false, "irq-phase-start");
    }
}

fn finish_phase(index: usize) -> u32 {
    let (mut end, mut pending) = (0, 0);
    check(
        unsafe { mp_irq_probe_stop(&mut end, &mut pending) } == 0,
        "irq-phase-stop",
    );
    compiler_fence(Ordering::Acquire);
    unsafe {
        let p = &mut *PROBE.0.get();
        check(p.active == index, "irq-phase-state");
        p.active = 2;
        p.phases[index].finish(end, pending != 0);
        p.phases[index].error
    }
}

pub fn start_idle() {
    start_phase(0);
}
pub fn finish_idle() {
    let error = finish_phase(0);
    let observed = unsafe { (*PROBE.0.get()).phases[0].observed };
    if error != 0 || observed == 0 {
        let _ = unsafe { mp_irq_probe_close() };
        check(false, "irq-idle-control");
    }
}
pub fn start() {
    start_phase(1);
}
pub fn finish() {
    let error = finish_phase(1);
    check(unsafe { mp_irq_probe_close() } == 0, "irq-observer-release");
    unsafe {
        (*PROBE.0.get()).closed = true;
    }
    check(error == 0, "irq-loaded-protocol");
}

pub fn emit() {
    let p = unsafe { &*PROBE.0.get() };
    check(
        p.opened && p.closed && p.active == 2,
        "irq-observer-quiescent",
    );
    let i = p.info;
    line(format_args!(
        "MPSC {{\"record\":\"irq-observer\",\"scope\":\"hardware-alarm-to-probe-entry\",\"counter_hz\":{},\"target\":1,\"unit\":0,\"priority\":1,\"cpu_interrupt\":{},\"core\":{},\"source\":{},\"isr_address\":{},\"callback_address\":{},\"read_address\":{},\"arm_address\":{},\"roots_internal\":true,\"cleaned_up\":true,\"external_clock_calibrated\":false,\"sample_capacity\":{},\"sample_storage_bytes\":{},\"rust_state_bytes\":{},\"sample_policy\":\"first-samples-plus-all-event-max\"}}",
        HZ,
        i[0],
        i[1],
        i[2],
        i[3],
        i[4],
        i[5],
        i[6],
        CAPACITY,
        2 * core::mem::size_of_val(&p.phases[0].samples),
        core::mem::size_of::<Probe>()
    ));
    for (name, phase) in ["idle", "loaded"].into_iter().zip(&p.phases) {
        line(format_args!(
            "MPSC {{\"record\":\"irq-phase\",\"phase\":\"{}\",\"start\":{},\"end\":{},\"period_ticks\":{},\"observed\":{},\"missed_periods\":{},\"trailing_periods\":{},\"stored\":{},\"dropped\":{},\"maximum_lateness_ticks\":{},\"pending_at_stop\":{},\"error\":{}}}",
            name,
            phase.start,
            phase.end,
            PERIOD,
            phase.observed,
            phase.missed,
            phase.trailing,
            phase.stored,
            phase.observed - phase.stored as u32,
            phase.maximum,
            phase.pending,
            phase.error
        ));
        for (index, s) in phase.samples[..phase.stored].iter().enumerate() {
            line(format_args!(
                "MPSC {{\"record\":\"irq-sample\",\"phase\":\"{}\",\"index\":{},\"deadline\":{},\"entered\":{},\"lateness_ticks\":{},\"skipped_periods\":{}}}",
                name, index, s.deadline, s.entered, s.lateness, s.skipped
            ));
        }
    }
}

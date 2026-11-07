//! Fixed-size native alarm source. ISR callbacks only timestamp/rearm and
//! perform at most four native gate posts; channel admission stays in tasks.
use crate::{check, engine::Mode, line, platform as p};
use core::sync::atomic::{AtomicU32, Ordering::*};
const RELEASE: usize = 16;
const LIMIT: usize = 256;
struct Edge {
    start: AtomicU32,
    end: AtomicU32,
}
static EDGES: [Edge; LIMIT] = [const {
    Edge {
        start: AtomicU32::new(0),
        end: AtomicU32::new(0),
    }
}; LIMIT];
static TOTAL: AtomicU32 = AtomicU32::new(0);
static NEXT: AtomicU32 = AtomicU32::new(0);
static PRODUCERS: AtomicU32 = AtomicU32::new(0);
static BATCH: AtomicU32 = AtomicU32::new(0);
static DEADLINE: AtomicU32 = AtomicU32::new(0);
fn batch(mode: Mode) -> usize {
    match mode {
        Mode::Paced => 1,
        Mode::Burst => 16,
        Mode::Overload => 64,
        _ => 0,
    }
}

extern "C" fn tick() {
    let index = NEXT.fetch_add(1, Relaxed) as usize;
    if index >= TOTAL.load(Acquire) as usize {
        p::fatal();
    }
    EDGES[index].start.store(p::clock(), Release);
    let n = PRODUCERS.load(Acquire) as usize;
    if BATCH.load(Acquire) == 1 {
        p::irq_post(RELEASE + index % n);
    } else {
        for producer in 0..n {
            p::irq_post(RELEASE + producer);
        }
    }
    if index + 1 < TOTAL.load(Acquire) as usize {
        let next = DEADLINE.fetch_add(1, Relaxed) + 1;
        p::irq_arm(next);
    }
    EDGES[index].end.store(p::clock(), Release);
}
pub fn prepare(mode: Mode, n: usize, samples: usize) {
    let b = batch(mode);
    let groups = if b == 0 {
        0
    } else if b == 1 {
        samples
    } else {
        samples.div_ceil(b * n)
    };
    check(groups <= LIMIT, "release-source-storage");
    TOTAL.store(groups as u32, Release);
    PRODUCERS.store(n as u32, Release);
    BATCH.store(b as u32, Release);
    if groups != 0 {
        p::pulse_init(tick);
    }
}
pub fn start() {
    if TOTAL.load(Acquire) != 0 {
        let now = p::native_ticks();
        check(now < u32::MAX as u64 - 256, "native-tick-window");
        DEADLINE.store(now as u32 + 2, Release);
        p::irq_arm(now as u32 + 2);
    }
}
pub fn release(mode: Mode, index: usize, n: usize) -> Option<u32> {
    let b = batch(mode);
    if b == 0 {
        return None;
    }
    let group = if b == 1 { index } else { index / (b * n) };
    if b == 1 || (index / n) % b == 0 {
        p::gate_wait(RELEASE + index % n);
    }
    Some(EDGES[group].start.load(Acquire))
}
pub fn finish() -> usize {
    let groups = TOTAL.load(Acquire) as usize;
    if groups != 0 {
        p::pulse_stop();
        check(
            NEXT.load(Acquire) as usize == groups,
            "missing/extra release",
        );
    }
    groups
}
pub fn emit() {
    for index in 0..TOTAL.load(Acquire) as usize {
        line(format_args!(
            "MPSC {{\"record\":\"release\",\"index\":{},\"start\":{},\"end\":{}}}",
            index,
            EDGES[index].start.load(Acquire),
            EDGES[index].end.load(Acquire)
        ));
    }
}

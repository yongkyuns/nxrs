//! Benchmark-only diagnostics, shared by Embassy and host tests.
use crate::protocol::*;
pub const EMPTY_FLOW: Flow = Flow {
    count: 0,
    digest: 0,
    last_sequence: 0,
    initialized: 0,
};
pub const EMPTY_STATE: State = State {
    received: [EMPTY_FLOW; 9],
    value: 0,
    errors: 0,
};
pub const EMPTY_HIST: Histogram = Histogram {
    bins: [0; 64],
    count: 0,
    maximum: 0,
};
#[repr(C)]
pub struct Diagnostics {
    pub accepted: [Flow; 9],
    pub publication: Histogram,
    pub start: Histogram,
    pub finish: Histogram,
    pub control_start: Histogram,
    pub queue_start: Histogram,
    pub attempted: u32,
    pub rejected: u32,
    pub errors: u32,
    pub missed: [u32; 3],
    pub last_finish: u32,
    pub pending: u32,
}
impl Diagnostics {
    pub const fn new() -> Self {
        Self {
            accepted: [EMPTY_FLOW; 9],
            publication: EMPTY_HIST,
            start: EMPTY_HIST,
            finish: EMPTY_HIST,
            control_start: EMPTY_HIST,
            queue_start: EMPTY_HIST,
            attempted: 0,
            rejected: 0,
            errors: 0,
            missed: [0; 3],
            last_finish: 0,
            pending: 0,
        }
    }
    pub fn send(&mut self, event: &Event, result: i32, posted: u32) {
        self.attempted += 1;
        self.publication
            .add(micros(posted - event.scheduled_cycles));
        if result == 1 {
            self.rejected += 1;
            return;
        }
        if result != 0 {
            self.errors += 1;
            return;
        }
        let flow = &mut self.accepted[event.peer as usize * 3 + event.kind as usize];
        flow.count += 1;
        flow.digest = flow.digest.wrapping_add(event.token);
        flow.initialized = 1;
        flow.last_sequence = event.sequence;
    }
    pub fn receive(&mut self, event: &Event, result: bool, started: u32, finished: u32) {
        if !result {
            self.errors += 1;
            return;
        }
        let start = micros(started - event.scheduled_cycles);
        let finish = micros(finished - event.scheduled_cycles);
        self.start.add(start);
        self.finish.add(finish);
        self.queue_start.add(micros(started - event.posted_cycles));
        if event.kind == 0 {
            self.control_start.add(start);
        }
        if finish > [20000, 40000, 80000][event.kind as usize] {
            self.missed[event.kind as usize] += 1;
        }
        self.last_finish = self.last_finish.max(finished);
    }
}
pub fn micros(cycles: u32) -> u32 {
    cycles / ES_HZ + u32::from(cycles % ES_HZ != 0)
}
pub fn merge(to: &mut Histogram, from: &Histogram) {
    for i in 0..64 {
        to.bins[i] += from.bins[i];
    }
    to.count += from.count;
    to.maximum = to.maximum.max(from.maximum);
}

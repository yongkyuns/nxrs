//! Bounded alarm accounting, independent of the interrupt controller ABI.
pub const HZ: u32 = 16_000_000;
pub const PERIOD: u32 = 1_600;
pub const CAPACITY: usize = 256;
pub const MASK: u64 = (1u64 << 52) - 1;

#[derive(Clone, Copy)]
pub struct Sample {
    pub deadline: u64,
    pub entered: u64,
    pub lateness: u32,
    pub skipped: u32,
}
impl Sample {
    const EMPTY: Self = Self {
        deadline: 0,
        entered: 0,
        lateness: 0,
        skipped: 0,
    };
}

pub struct Phase {
    pub start: u64,
    pub end: u64,
    pub next: u64,
    pub observed: u32,
    pub missed: u32,
    pub trailing: u32,
    pub stored: usize,
    pub maximum: u32,
    pub pending: bool,
    pub error: u32,
    pub samples: [Sample; CAPACITY],
}
impl Phase {
    pub const fn new() -> Self {
        Self {
            start: 0,
            end: 0,
            next: 0,
            observed: 0,
            missed: 0,
            trailing: 0,
            stored: 0,
            maximum: 0,
            pending: false,
            error: 0,
            samples: [Sample::EMPTY; CAPACITY],
        }
    }
    pub fn begin(&mut self, now: u64) {
        self.start = now;
        self.next = (now + u64::from(PERIOD)) & MASK;
    }
    #[inline(always)]
    pub fn entry(&mut self, entered: u64) -> Option<u64> {
        let late = entered.wrapping_sub(self.next) & MASK;
        // Capture windows are under eight seconds. Reject early, invalid or
        // ambiguous counter readings instead of manufacturing a latency.
        if entered > MASK || late >= (1 << 31) || self.error != 0 {
            self.error = 1;
            return None;
        }
        let late = late as u32;
        let skipped = late / PERIOD;
        if self.stored < CAPACITY {
            self.samples[self.stored] = Sample {
                deadline: self.next,
                entered,
                lateness: late,
                skipped,
            };
            self.stored += 1;
        }
        self.observed += 1;
        self.missed += skipped;
        self.maximum = self.maximum.max(late);
        // Preserve the absolute cadence. Do not shift every subsequent alarm
        // by this handler's lateness, or loop once per missed period in the ISR.
        self.next = (self.next + u64::from((skipped + 1) * PERIOD)) & MASK;
        Some(self.next)
    }
    pub fn finish(&mut self, end: u64, pending: bool) {
        self.end = end;
        self.pending = pending;
        let elapsed = end.wrapping_sub(self.start) & MASK;
        if end > MASK || elapsed >= (1 << 31) {
            self.error = 2;
            return;
        }
        let due = elapsed as u32 / PERIOD;
        if let Some(trailing) = due.checked_sub(self.observed + self.missed) {
            self.trailing = trailing;
            if pending && trailing == 0 {
                self.error = 3;
            }
        } else {
            self.error = 4;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cadence_skips_late_periods_without_accumulating_phase_drift() {
        let mut phase = Phase::new();
        phase.begin(100);
        assert_eq!(
            phase.entry(100 + 3 * u64::from(PERIOD) + 7),
            Some(100 + 4 * u64::from(PERIOD))
        );
        assert_eq!(
            (phase.observed, phase.missed, phase.maximum),
            (1, 2, 2 * PERIOD + 7)
        );
        phase.finish(100 + 5 * u64::from(PERIOD), true);
        assert_eq!((phase.trailing, phase.error), (2, 0));
    }
    #[test]
    fn counter_wrap_and_early_or_failed_reads_are_explicit() {
        let mut phase = Phase::new();
        phase.begin(MASK - 100);
        assert_eq!(phase.next, u64::from(PERIOD) - 101);
        assert_eq!(
            phase.entry(phase.next + 3),
            Some(2 * u64::from(PERIOD) - 101)
        );
        phase.finish(2 * u64::from(PERIOD) - 102, false);
        assert_eq!((phase.observed, phase.trailing, phase.error), (1, 0, 0));
        for entered in (0..1).chain(core::iter::once(u64::MAX)) {
            let mut phase = Phase::new();
            phase.begin(100);
            assert!(phase.entry(entered).is_none());
            assert_eq!(phase.observed, 0);
            assert_ne!(phase.error, 0);
        }
    }
    #[test]
    fn storage_overflow_keeps_all_event_counts_and_maximum() {
        let mut phase = Phase::new();
        phase.begin(0);
        for _ in 0..CAPACITY + 10 {
            assert!(phase.entry(phase.next).is_some());
        }
        assert!(phase.entry(phase.next + 5 * u64::from(PERIOD)).is_some());
        phase.finish(phase.next - 1, false);
        assert_eq!(phase.stored, CAPACITY);
        assert_eq!(phase.observed, CAPACITY as u32 + 11);
        assert_eq!(phase.maximum, 5 * PERIOD);
        assert_eq!((phase.missed, phase.trailing, phase.error), (5, 0, 0));
    }
    #[test]
    fn idle_stop_before_first_alarm_and_missing_delivery_are_distinct() {
        let mut phase = Phase::new();
        phase.begin(100);
        phase.finish(101, false);
        assert_eq!((phase.observed, phase.trailing, phase.error), (0, 0, 0));
        let mut phase = Phase::new();
        phase.begin(100);
        phase.finish(100 + 4 * u64::from(PERIOD), true);
        assert_eq!((phase.observed, phase.trailing, phase.error), (0, 4, 0));
    }
}

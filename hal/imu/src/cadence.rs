//! Absolute, coalescing acquisition deadlines. A tick never implies valid data.
use std::time::{Duration, Instant};

pub(crate) struct Cadence {
    period: Duration,
    next: Instant,
}
impl Cadence {
    pub(crate) fn new(now: Instant, period: Duration) -> Self {
        assert!(!period.is_zero());
        Self {
            period,
            next: now + period,
        }
    }
    pub(crate) fn remaining(&self, now: Instant) -> Duration {
        self.next.saturating_duration_since(now)
    }
    pub(crate) fn take_due(&mut self, now: Instant) -> Option<Instant> {
        if now < self.next {
            return None;
        }
        let due = self.next;
        self.next += self.period;
        if self.next <= now {
            self.next = now + self.period;
        }
        Some(due)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn early_wakes_keep_the_deadline_and_late_wakes_do_not_accumulate_ticks() {
        let start = Instant::now();
        let period = Duration::from_millis(10);
        let mut clock = Cadence::new(start, period);
        assert_eq!(clock.take_due(start + Duration::from_millis(9)), None);
        assert_eq!(
            clock.remaining(start + Duration::from_millis(9)),
            Duration::from_millis(1)
        );
        assert_eq!(clock.take_due(start + period), Some(start + period));
        assert_eq!(clock.remaining(start + period), period);
        assert_eq!(
            clock.take_due(start + period * 10),
            Some(start + period * 2)
        );
        assert_eq!(clock.take_due(start + period * 10), None);
        assert_eq!(clock.remaining(start + period * 10), period);
    }
}

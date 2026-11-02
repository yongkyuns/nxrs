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
    pub(crate) fn take_due(&mut self, now: Instant) -> bool {
        if now < self.next {
            return false;
        }
        self.next += self.period;
        if self.next <= now {
            self.next = now + self.period;
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn early_wakes_keep_the_deadline_and_late_wakes_do_not_accumulate_ticks() {
        let start = Instant::now();
        let period = crate::bmi270::FIFO_WAKE_PERIOD;
        assert_eq!(period, Duration::from_millis(40));
        let mut clock = Cadence::new(start, period);
        assert!(!clock.take_due(start + Duration::from_millis(39)));
        assert_eq!(
            clock.remaining(start + Duration::from_millis(39)),
            Duration::from_millis(1)
        );
        assert!(clock.take_due(start + period));
        assert_eq!(clock.remaining(start + period), period);
        assert!(clock.take_due(start + period * 10));
        assert!(!clock.take_due(start + period * 10));
        assert_eq!(clock.remaining(start + period * 10), period);
    }
}

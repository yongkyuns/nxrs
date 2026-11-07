use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Decision {
    PauseGnss,
    ResumeGnss,
    Stop,
}

pub(super) struct Schedule {
    duration: Duration,
    phases: [(Duration, Decision); 2],
    next: usize,
    stopped: bool,
}

impl Schedule {
    pub(super) fn new(duration: Duration) -> Self {
        let total_ms = u64::try_from(duration.as_millis()).unwrap_or(u64::MAX);
        let pause_at = Duration::from_millis((total_ms / 3).max(1));
        let resume_at = Duration::from_millis((total_ms.saturating_mul(2) / 3).max(2));
        Self {
            duration,
            phases: [
                (pause_at, Decision::PauseGnss),
                (resume_at, Decision::ResumeGnss),
            ],
            next: 0,
            stopped: false,
        }
    }

    /// Take the next due decision. The stop deadline ends the observation window.
    pub(super) fn advance(&mut self, elapsed: Duration) -> Option<Decision> {
        if self.stopped {
            return None;
        }
        if elapsed >= self.duration {
            self.stopped = true;
            return Some(Decision::Stop);
        }
        let &(deadline, decision) = self.phases.get(self.next)?;
        if elapsed < deadline {
            return None;
        }
        self.next += 1;
        Some(decision)
    }
}

#[cfg(test)]
mod tests {
    use super::{Decision, Schedule};
    use std::time::Duration;

    fn decisions(duration_ms: u64, wakes_ms: &[u64]) -> Vec<Decision> {
        let mut schedule = Schedule::new(Duration::from_millis(duration_ms));
        let mut decisions = Vec::new();
        for &wake in wakes_ms {
            while let Some(decision) = schedule.advance(Duration::from_millis(wake)) {
                decisions.push(decision);
            }
        }
        decisions
    }

    #[test]
    fn phase_decisions_are_stable_across_wake_schedules_and_emitted_once() {
        let fine_grained = decisions(2_000, &[0, 300, 666, 900, 1_333, 1_700, 2_000]);
        let delayed = decisions(2_000, &[0, 999, 1_600, 2_005]);
        let coalesced = decisions(2_000, &[0, 1_700, 2_000, 2_000]);
        assert_eq!(fine_grained, delayed);
        assert_eq!(fine_grained, coalesced);
        assert_eq!(
            fine_grained,
            [Decision::PauseGnss, Decision::ResumeGnss, Decision::Stop]
        );
    }

    #[test]
    fn stop_is_terminal_and_short_durations_keep_existing_phase_boundaries() {
        assert_eq!(
            decisions(2_000, &[666, 1_333, 2_000, 2_000]),
            [Decision::PauseGnss, Decision::ResumeGnss, Decision::Stop]
        );
        assert_eq!(decisions(1, &[0, 1]), [Decision::Stop]);
        assert_eq!(decisions(2, &[1, 2]), [Decision::PauseGnss, Decision::Stop]);
    }
}

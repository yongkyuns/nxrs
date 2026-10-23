//! Monotonic decoder evidence, retained across source attempts in one worker.
use serde::{Deserialize, Serialize};
use std::time::Duration;
use tokio::time::Instant;

#[derive(Clone, Default, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProgressHealth {
    pub observer_run: u64,
    pub recoveries: u64,
    pub last_recovery_ms: Option<u64>,
    pub max_recovery_ms: u64,
    pub max_frame_gap_ms: u64,
    pub frame_gaps_over_30s: u64,
}

pub(crate) struct FrameProgress {
    health: ProgressHealth,
    last_frame: Option<Instant>,
    recovering: bool,
}

impl FrameProgress {
    pub(crate) fn new(previous: Option<&ProgressHealth>) -> Self {
        Self {
            health: ProgressHealth {
                observer_run: previous.map_or(1, |v| v.observer_run.saturating_add(1)),
                ..Default::default()
            },
            last_frame: None,
            recovering: false,
        }
    }

    pub(crate) fn frame(&mut self, now: Instant) {
        if let Some(previous) = self.last_frame.replace(now) {
            let gap = now.saturating_duration_since(previous);
            let millis = u64::try_from(gap.as_millis()).unwrap_or(u64::MAX);
            self.health.max_frame_gap_ms = self.health.max_frame_gap_ms.max(millis);
            if gap > Duration::from_secs(30) {
                self.health.frame_gaps_over_30s = self.health.frame_gaps_over_30s.saturating_add(1);
            }
            if self.recovering {
                self.health.recoveries = self.health.recoveries.saturating_add(1);
                self.health.last_recovery_ms = Some(millis);
                self.health.max_recovery_ms = self.health.max_recovery_ms.max(millis);
            }
        }
        self.recovering = false;
    }

    pub(crate) fn source_retired(&mut self) {
        // Initial acquisition is not a recovery. Failed retries retain the last
        // decoded frame so cleanup, backoff and all attempts count in the gap.
        self.recovering = self.last_frame.is_some();
    }

    pub(crate) fn health(&self) -> ProgressHealth {
        self.health.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovery_includes_failed_attempts_and_ends_at_first_decoded_frame() {
        let start = Instant::now();
        let mut progress = FrameProgress::new(None);
        progress.source_retired();
        progress.frame(start);
        assert_eq!(progress.health().recoveries, 0);
        progress.frame(start + Duration::from_millis(500));
        progress.source_retired();
        progress.source_retired();
        progress.frame(start + Duration::from_secs(41));
        let health = progress.health();
        assert_eq!(health.recoveries, 1);
        assert_eq!(health.last_recovery_ms, Some(40_500));
        assert_eq!(health.max_recovery_ms, 40_500);
        assert_eq!(health.max_frame_gap_ms, 40_500);
        assert_eq!(health.frame_gaps_over_30s, 1);
        progress.frame(start + Duration::from_secs(42));
        progress.source_retired();
        progress.frame(start + Duration::from_secs(45));
        assert_eq!(progress.health().recoveries, 2);
        assert_eq!(progress.health().last_recovery_ms, Some(3_000));
        assert_eq!(progress.health().max_recovery_ms, 40_500);
    }

    #[test]
    fn stalled_decoder_is_evidence_even_without_a_reader_restart() {
        let start = Instant::now();
        let mut progress = FrameProgress::new(None);
        progress.frame(start);
        progress.frame(start + Duration::from_secs(31));
        assert_eq!(progress.health().recoveries, 0);
        assert_eq!(progress.health().last_recovery_ms, None);
        assert_eq!(progress.health().max_frame_gap_ms, 31_000);
        assert_eq!(progress.health().frame_gaps_over_30s, 1);
    }

    #[test]
    fn process_restart_opens_an_explicit_new_measurement_interval() {
        let previous = ProgressHealth {
            observer_run: 7,
            recoveries: 4,
            max_frame_gap_ms: 90_000,
            ..Default::default()
        };
        let mut progress = FrameProgress::new(Some(&previous));
        progress.frame(Instant::now());
        assert_eq!(progress.health().observer_run, 8);
        assert_eq!(progress.health().recoveries, 0);
        assert_eq!(progress.health().last_recovery_ms, None);
        assert_eq!(progress.health().max_frame_gap_ms, 0);
    }
}

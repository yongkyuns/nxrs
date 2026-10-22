//! Caller-polled policy for experimental presence-classifier reports.
//!
//! This does not extract CSI or classify RF data. A sensor supplies calibrated
//! hints; Rust owns their debounce, hold and report-stream freshness. Quiet
//! means no evidence was detected, never proof that a room is empty. A fresh
//! report cannot establish RF freshness unless its source measures that too.
#![no_std]
#![forbid(unsafe_code)]

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub confirm_samples: u16,
    pub confirm_for_ms: u64,
    pub hold_ms: u64,
    pub stale_after_ms: u64,
    pub max_sample_gap_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            confirm_samples: 3,
            confirm_for_ms: 100,
            hold_ms: 10_000,
            stale_after_ms: 2_000,
            max_sample_gap_ms: 250,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidConfig,
    ClockWentBackwards,
    /// Duplicate reports and a source reboot both require new progress. A
    /// reconnect/boot handshake must explicitly begin a new session.
    SourceDidNotAdvance,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Calibration {
    Required,
    InProgress,
    Ready,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Sample {
    /// Strictly increasing within one source session; not the observer clock.
    pub source_timestamp_ms: u64,
    pub calibration: Calibration,
    pub motion_detected: bool,
    pub presence_detected: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Evidence {
    Motion,
    Presence,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UnknownReason {
    WaitingForSamples,
    CalibrationRequired,
    Calibrating,
    SignalLost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Unknown(UnknownReason),
    NotDetected,
    Detected(Evidence),
}

#[derive(Clone, Copy)]
struct Candidate {
    detected: bool,
    since_ms: u64,
    samples: u16,
}

/// Constant storage, no allocation, worker, queue, blocking call or HAL access.
/// One caller owns this tracker and supplies a monotonic observer clock. Call
/// `poll` at `next_deadline_ms` even when the source becomes silent.
pub struct Tracker {
    config: Config,
    state: State,
    now_ms: u64,
    source_timestamp_ms: Option<u64>,
    last_report_ms: Option<u64>,
    last_positive_ms: Option<u64>,
    candidate: Option<Candidate>,
}

impl Tracker {
    pub fn new(config: Config) -> Result<Self, Error> {
        if !(2..=1_000).contains(&config.confirm_samples)
            || config.confirm_for_ms == 0
            || config.confirm_for_ms >= config.stale_after_ms
            || config.max_sample_gap_ms == 0
            || config.max_sample_gap_ms > config.stale_after_ms
            || config.stale_after_ms > 3_600_000
            || config.hold_ms > 3_600_000
        {
            return Err(Error::InvalidConfig);
        }
        Ok(Self {
            config,
            state: State::Unknown(UnknownReason::WaitingForSamples),
            now_ms: 0,
            source_timestamp_ms: None,
            last_report_ms: None,
            last_positive_ms: None,
            candidate: None,
        })
    }

    pub fn state(&self) -> State {
        self.state
    }

    pub fn next_deadline_ms(&self) -> Option<u64> {
        self.last_report_ms
            .and_then(|last| last.checked_add(self.config.stale_after_ms))
    }

    /// A new connection/boot invalidates old presence and debounce history.
    /// The caller must have retired the old input before accepting a new one.
    pub fn begin_session(&mut self, now_ms: u64) -> Result<State, Error> {
        self.check_clock(now_ms)?;
        self.invalidate(UnknownReason::WaitingForSamples);
        self.source_timestamp_ms = None;
        Ok(self.state)
    }

    pub fn source_lost(&mut self, now_ms: u64) -> Result<State, Error> {
        self.check_clock(now_ms)?;
        self.invalidate(UnknownReason::SignalLost);
        Ok(self.state)
    }

    /// The deadline is inclusive. Duplicate/reordered source timestamps never
    /// renew it, including when `on_sample` returns an error.
    pub fn poll(&mut self, now_ms: u64) -> Result<State, Error> {
        self.check_clock(now_ms)?;
        if self
            .last_report_ms
            .is_some_and(|last| now_ms - last >= self.config.stale_after_ms)
        {
            self.invalidate(UnknownReason::SignalLost);
        }
        Ok(self.state)
    }

    pub fn on_sample(&mut self, sample: Sample, now_ms: u64) -> Result<State, Error> {
        self.poll(now_ms)?;
        if self
            .source_timestamp_ms
            .is_some_and(|last| sample.source_timestamp_ms <= last)
        {
            return Err(Error::SourceDidNotAdvance);
        }
        if self
            .last_report_ms
            .is_some_and(|last| now_ms - last > self.config.max_sample_gap_ms)
        {
            self.candidate = None;
        }
        self.source_timestamp_ms = Some(sample.source_timestamp_ms);
        self.last_report_ms = Some(now_ms);
        match sample.calibration {
            Calibration::Required | Calibration::InProgress => {
                self.state = State::Unknown(if sample.calibration == Calibration::Required {
                    UnknownReason::CalibrationRequired
                } else {
                    UnknownReason::Calibrating
                });
                self.last_positive_ms = None;
                self.candidate = None;
                return Ok(self.state);
            }
            Calibration::Ready => {}
        }

        let detected = sample.motion_detected || sample.presence_detected;
        let candidate = match self.candidate {
            Some(mut value) if value.detected == detected => {
                value.samples = value.samples.saturating_add(1);
                value
            }
            _ => Candidate {
                detected,
                since_ms: now_ms,
                samples: 1,
            },
        };
        self.candidate = Some(candidate);
        let confirmed = candidate.samples >= self.config.confirm_samples
            && now_ms - candidate.since_ms >= self.config.confirm_for_ms;

        if detected {
            if matches!(self.state, State::Detected(_)) || confirmed {
                self.state = State::Detected(if sample.motion_detected {
                    Evidence::Motion
                } else {
                    Evidence::Presence
                });
                self.last_positive_ms = Some(now_ms);
            }
        } else if confirmed
            && self
                .last_positive_ms
                .is_none_or(|last| now_ms - last >= self.config.hold_ms)
        {
            self.state = State::NotDetected;
        }
        Ok(self.state)
    }

    fn check_clock(&mut self, now_ms: u64) -> Result<(), Error> {
        if now_ms < self.now_ms {
            return Err(Error::ClockWentBackwards);
        }
        self.now_ms = now_ms;
        Ok(())
    }

    fn invalidate(&mut self, reason: UnknownReason) {
        self.state = State::Unknown(reason);
        self.last_report_ms = None;
        self.last_positive_ms = None;
        self.candidate = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tracker() -> Tracker {
        Tracker::new(Config::default()).unwrap()
    }

    fn sample(timestamp: u64, present: bool) -> Sample {
        Sample {
            source_timestamp_ms: timestamp,
            calibration: Calibration::Ready,
            motion_detected: false,
            presence_detected: present,
        }
    }

    fn feed(tracker: &mut Tracker, now: u64, present: bool) -> State {
        tracker.on_sample(sample(now, present), now).unwrap()
    }

    #[test]
    fn confirmation_requires_both_time_and_samples() {
        let mut t = tracker();
        feed(&mut t, 0, true);
        feed(&mut t, 1, true);
        assert!(matches!(feed(&mut t, 2, true), State::Unknown(_)));
        assert_eq!(feed(&mut t, 100, true), State::Detected(Evidence::Presence));
        let mut t = tracker();
        feed(&mut t, 0, true);
        assert!(matches!(feed(&mut t, 100, true), State::Unknown(_)));
        assert_eq!(feed(&mut t, 150, true), State::Detected(Evidence::Presence));
    }

    #[test]
    fn a_single_spike_does_not_detect_presence() {
        let mut t = tracker();
        for now in [0, 50, 100] {
            feed(&mut t, now, false);
        }
        assert_eq!(feed(&mut t, 150, true), State::NotDetected);
        for now in [200, 250, 300] {
            assert_eq!(feed(&mut t, now, false), State::NotDetected);
        }
    }

    #[test]
    fn calibration_invalidates_old_presence_immediately() {
        for calibration in [Calibration::Required, Calibration::InProgress] {
            let mut t = tracker();
            for now in [0, 50, 100] {
                feed(&mut t, now, true);
            }
            let mut value = sample(150, true);
            value.calibration = calibration;
            assert!(matches!(
                t.on_sample(value, 150).unwrap(),
                State::Unknown(_)
            ));
            assert!(matches!(feed(&mut t, 200, true), State::Unknown(_)));
        }
    }

    #[test]
    fn hold_expires_only_with_confirmed_fresh_negative_evidence() {
        let mut t = tracker();
        for now in [0, 50, 100] {
            feed(&mut t, now, true);
        }
        for now in (150..10_100).step_by(50) {
            assert_eq!(
                feed(&mut t, now, false),
                State::Detected(Evidence::Presence)
            );
        }
        assert_eq!(feed(&mut t, 10_100, false), State::NotDetected);
    }

    #[test]
    fn loss_is_unknown_even_during_hold_and_clears_confirmation() {
        let mut t = tracker();
        for now in [0, 50, 100] {
            feed(&mut t, now, true);
        }
        assert_eq!(t.next_deadline_ms(), Some(2_100));
        assert_eq!(t.poll(2_099).unwrap(), State::Detected(Evidence::Presence));
        assert_eq!(
            t.poll(2_100).unwrap(),
            State::Unknown(UnknownReason::SignalLost)
        );
        assert_eq!(t.next_deadline_ms(), None);
        assert!(matches!(feed(&mut t, 2_150, true), State::Unknown(_)));
    }

    #[test]
    fn duplicate_or_reordered_reports_never_renew_freshness() {
        let mut t = tracker();
        feed(&mut t, 100, false);
        assert_eq!(
            t.on_sample(sample(100, true), 1_000),
            Err(Error::SourceDidNotAdvance)
        );
        assert_eq!(
            t.on_sample(sample(99, true), 2_100),
            Err(Error::SourceDidNotAdvance)
        );
        assert_eq!(t.state(), State::Unknown(UnknownReason::SignalLost));
    }

    #[test]
    fn long_inter_sample_gaps_do_not_form_a_confirmation() {
        let mut t = tracker();
        for now in [0, 500, 1_000, 1_500] {
            assert!(matches!(feed(&mut t, now, true), State::Unknown(_)));
        }
        feed(&mut t, 1_550, true);
        assert_eq!(
            feed(&mut t, 1_600, true),
            State::Detected(Evidence::Presence)
        );
    }

    #[test]
    fn motion_is_distinct_from_a_presence_hint() {
        let mut t = tracker();
        for now in [0, 50, 100] {
            let mut value = sample(now, false);
            value.motion_detected = true;
            t.on_sample(value, now).unwrap();
        }
        assert_eq!(t.state(), State::Detected(Evidence::Motion));
        assert_eq!(feed(&mut t, 150, true), State::Detected(Evidence::Presence));
    }

    #[test]
    fn explicit_new_session_accepts_rebooted_clock_without_old_evidence() {
        let mut t = tracker();
        for now in [1_000, 1_050, 1_100] {
            feed(&mut t, now, true);
        }
        assert_eq!(
            t.on_sample(sample(0, true), 1_150),
            Err(Error::SourceDidNotAdvance)
        );
        t.begin_session(1_200).unwrap();
        assert_eq!(t.state(), State::Unknown(UnknownReason::WaitingForSamples));
        assert!(matches!(
            t.on_sample(sample(0, true), 1_250).unwrap(),
            State::Unknown(_)
        ));
    }

    #[test]
    fn reversed_observer_clock_is_rejected_without_mutation() {
        let mut t = tracker();
        feed(&mut t, 100, false);
        assert_eq!(t.poll(99), Err(Error::ClockWentBackwards));
        assert_eq!(t.begin_session(98), Err(Error::ClockWentBackwards));
        assert_eq!(
            t.on_sample(sample(101, true), 97),
            Err(Error::ClockWentBackwards)
        );
        assert_eq!(t.next_deadline_ms(), Some(2_100));
    }

    #[test]
    fn explicit_input_close_invalidates_presence() {
        let mut t = tracker();
        for now in [0, 50, 100] {
            feed(&mut t, now, true);
        }
        assert_eq!(
            t.source_lost(150).unwrap(),
            State::Unknown(UnknownReason::SignalLost)
        );
    }

    #[test]
    fn rejects_unbounded_or_inconsistent_config() {
        for value in [
            Config {
                confirm_samples: 1,
                ..Config::default()
            },
            Config {
                confirm_for_ms: 0,
                ..Config::default()
            },
            Config {
                confirm_for_ms: 2_000,
                ..Config::default()
            },
            Config {
                max_sample_gap_ms: 2_001,
                ..Config::default()
            },
            Config {
                hold_ms: u64::MAX,
                ..Config::default()
            },
        ] {
            assert!(matches!(Tracker::new(value), Err(Error::InvalidConfig)));
        }
    }
}

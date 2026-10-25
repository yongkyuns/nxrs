//! Original Nxrs experiment policy over unchanged Espressif numerical features.
//! No production provider and no third-party detector code or model weights.
//! Learn the *detection-time* statistic after vendor training has stopped, then
//! require a separate labelled empty holdout. Never adapt to the occupied phase.
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, TryLockError};

pub const HISTORY: usize = 25;
#[derive(Clone, Copy)]
pub struct History {
    pub phase: Phase,
    pub rejection: Rejection,
    pub features: u32,
    pub filled: usize,
    pub index: usize,
    pub average: f32,
    pub lower: f32,
    pub upper: f32,
    pub enter: f32,
    pub exit: f32,
    pub values: [f32; HISTORY],
    pub sequences: [u32; HISTORY],
    pub background_ready: bool,
}
impl History {
    pub const ZERO: Self = Self {
        phase: Phase::Uncalibrated,
        rejection: Rejection::None,
        features: 0,
        filled: 0,
        index: 0,
        average: 0.0,
        lower: 0.0,
        upper: 0.0,
        enter: 0.0,
        exit: 0.0,
        values: [0.0; HISTORY],
        sequences: [0; HISTORY],
        background_ready: false,
    };
}

const LOWER: usize = 13;

#[derive(Clone, Copy)]
pub struct Config {
    pub minimum_features: u32,
    pub minimum_duration_us: u64,
    pub freshness_us: u64,
    pub sensitivity: f32,
    pub minimum_margin: f32,
}
impl Config {
    /// Named qualification case, not a product tuning recommendation.
    pub const EMPTY_HOLDOUT: Self = Self {
        minimum_features: 128,
        minimum_duration_us: 15_000_000,
        freshness_us: 1_000_000,
        sensitivity: 0.25,
        minimum_margin: 0.000001,
    };
    fn valid(self) -> bool {
        self.minimum_features >= HISTORY as u32
            && self.minimum_duration_us > 0
            && self.freshness_us > 0
            && self.sensitivity.is_finite()
            && (0.0..=1.0).contains(&self.sensitivity)
            && self.minimum_margin.is_finite()
            && self.minimum_margin > 0.0
            && self.minimum_margin < 1.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Phase {
    Uncalibrated,
    Background,
    Holdout,
    Ready,
    Rejected,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum State {
    Unknown,
    NotDetected,
    PresenceHint,
}
/// Stable diagnostic codes, independent of the event that retained a matrix.
#[derive(Clone, Copy, Debug, PartialEq)]
#[repr(u32)]
pub enum Rejection {
    None = 0,
    TraceFault = 1,
    NativeError = 2,
    SourceContention = 3,
    ControlContention = 4,
    Poisoned = 5,
    ClockRollback = 6,
    ProgressExpired = 7,
    InvalidFeature = 8,
    TrainingIncomplete = 9,
    GainUnavailable = 10,
    TemplatesChanged = 11,
    InvalidRfTime = 12,
    RfExpired = 13,
    SourceReversed = 14,
    SaturatedEnvelope = 15,
    HoldoutExceeded = 16,
}
impl Rejection {
    fn from_code(code: u32) -> Self {
        match code {
            0 => Self::None,
            1 => Self::TraceFault,
            2 => Self::NativeError,
            3 => Self::SourceContention,
            4 => Self::ControlContention,
            5 => Self::Poisoned,
            6 => Self::ClockRollback,
            7 => Self::ProgressExpired,
            8 => Self::InvalidFeature,
            9 => Self::TrainingIncomplete,
            10 => Self::GainUnavailable,
            11 => Self::TemplatesChanged,
            12 => Self::InvalidRfTime,
            13 => Self::RfExpired,
            14 => Self::SourceReversed,
            15 => Self::SaturatedEnvelope,
            16 => Self::HoldoutExceeded,
            _ => unreachable!("private rejection code"),
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::TraceFault => "trace_fault",
            Self::NativeError => "native_error",
            Self::SourceContention => "source_contention",
            Self::ControlContention => "control_contention",
            Self::Poisoned => "poisoned",
            Self::ClockRollback => "clock_rollback",
            Self::ProgressExpired => "progress_expired",
            Self::InvalidFeature => "invalid_feature",
            Self::TrainingIncomplete => "training_incomplete",
            Self::GainUnavailable => "gain_unavailable",
            Self::TemplatesChanged => "templates_changed",
            Self::InvalidRfTime => "invalid_rf_time",
            Self::RfExpired => "rf_expired",
            Self::SourceReversed => "source_reversed",
            Self::SaturatedEnvelope => "saturated_envelope",
            Self::HoldoutExceeded => "holdout_exceeded",
        }
    }
}
impl Phase {
    pub fn name(self) -> &'static str {
        match self {
            Self::Uncalibrated => "uncalibrated",
            Self::Background => "background",
            Self::Holdout => "holdout",
            Self::Ready => "ready",
            Self::Rejected => "rejected",
        }
    }
}
impl State {
    pub fn name(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::NotDetected => "not_detected",
            Self::PresenceHint => "presence_hint",
        }
    }
}
#[derive(Clone, Copy)]
pub struct Feature {
    pub sequence: u32,
    pub oldest_us: u64,
    pub newest_us: u64,
    pub templates: u32,
    pub training_complete: bool,
    pub gain_available: bool,
    pub wander: f32,
}
#[derive(Clone, Copy)]
pub struct Snapshot {
    pub phase: Phase,
    pub state: State,
    pub rejection: Rejection,
    pub features: u32,
    pub average: f32,
    pub lower: f32,
    pub upper: f32,
    pub enter: f32,
    pub exit: f32,
}
pub struct Calibration {
    config: Config,
    phase: Phase,
    state: State,
    rejection: Rejection,
    last_clock: u64,
    phase_started: u64,
    last_received: u64,
    last_source: Option<(u32, u64)>,
    templates: u32,
    history: [f32; HISTORY],
    history_sequences: [u32; HISTORY],
    filled: usize,
    index: usize,
    features: u32,
    average: f32,
    lower: f32,
    upper: f32,
    enter: f32,
    exit: f32,
}
impl Calibration {
    pub fn new(config: Config) -> Option<Self> {
        config.valid().then_some(Self {
            config,
            phase: Phase::Uncalibrated,
            state: State::Unknown,
            rejection: Rejection::None,
            last_clock: 0,
            phase_started: 0,
            last_received: 0,
            last_source: None,
            templates: 0,
            history: [0.0; HISTORY],
            history_sequences: [0; HISTORY],
            filled: 0,
            index: 0,
            features: 0,
            average: 0.0,
            lower: 0.0,
            upper: 0.0,
            enter: 0.0,
            exit: 0.0,
        })
    }
    pub fn reject(&mut self, reason: Rejection) {
        assert_ne!(reason, Rejection::None);
        if self.phase == Phase::Rejected {
            return;
        }
        self.rejection = reason;
        self.phase = Phase::Rejected;
        self.state = State::Unknown;
    }
    fn reset_history(&mut self, now: u64) {
        self.phase_started = now;
        self.last_clock = now;
        self.last_received = now;
        self.history.fill(0.0);
        self.history_sequences.fill(0);
        self.filled = 0;
        self.index = 0;
        self.features = 0;
        self.average = 0.0;
        self.state = State::Unknown;
    }
    pub fn start(&mut self, now: u64, templates: u32) -> bool {
        if self.phase != Phase::Uncalibrated || templates == 0 {
            return false;
        }
        self.templates = templates;
        self.phase = Phase::Background;
        self.reset_history(now);
        true
    }
    fn enough(&self, now: u64) -> bool {
        self.features >= self.config.minimum_features
            && now >= self.phase_started
            && now - self.phase_started >= self.config.minimum_duration_us
    }
    pub fn validate(&mut self, now: u64) -> bool {
        self.expire(now);
        if self.phase != Phase::Background || !self.enough(now) {
            return false;
        }
        // Sensitivity reduces the margin ABOVE observed background; unlike the
        // vendor rule it cannot lower the threshold beneath that background.
        let gain = 1.0 + 29.0 * self.config.sensitivity;
        self.enter = self.upper + ((self.upper - self.lower) + self.config.minimum_margin) / gain;
        self.exit = self.upper;
        if !self.enter.is_finite() || self.enter >= 1.0 || self.enter <= self.exit {
            self.reject(Rejection::SaturatedEnvelope);
            return false;
        }
        self.phase = Phase::Holdout;
        // New history is essential: calibration samples cannot populate the
        // validation statistic or count as independent held-out evidence.
        self.reset_history(now);
        true
    }
    pub fn accept(&mut self, now: u64) -> bool {
        self.expire(now);
        if self.phase != Phase::Holdout || !self.enough(now) {
            return false;
        }
        self.phase = Phase::Ready;
        self.state = State::Unknown; // Require a new feature after acceptance.
        true
    }
    pub fn observe(&mut self, feature: Feature, now: u64) {
        if matches!(self.phase, Phase::Uncalibrated | Phase::Rejected) {
            return;
        }
        self.expire(now);
        if self.phase == Phase::Rejected {
            return;
        }
        let reason = if !feature.wander.is_finite() || !(0.0..=1.0).contains(&feature.wander) {
            Rejection::InvalidFeature
        } else if !feature.training_complete {
            Rejection::TrainingIncomplete
        } else if !feature.gain_available {
            Rejection::GainUnavailable
        } else if feature.templates != self.templates {
            Rejection::TemplatesChanged
        } else if feature.oldest_us > feature.newest_us || feature.newest_us > now {
            Rejection::InvalidRfTime
        } else if now - feature.oldest_us > self.config.freshness_us {
            Rejection::RfExpired
        } else {
            Rejection::None
        };
        if reason != Rejection::None {
            self.reject(reason);
            return;
        }
        if let Some((sequence, source_us)) = self.last_source {
            let delta = feature.sequence.wrapping_sub(sequence);
            if delta == 0 || delta >= (1 << 31) || feature.newest_us <= source_us {
                self.reject(Rejection::SourceReversed);
                return;
            }
        }
        self.last_source = Some((feature.sequence, feature.newest_us));
        // Reject queued pre-command data as qualification evidence without
        // confusing normal overlapping/straddling windows with a radio fault.
        if feature.oldest_us < self.phase_started {
            return;
        }
        self.last_received = now;
        self.history[self.index] = feature.wander;
        self.history_sequences[self.index] = feature.sequence;
        self.index = (self.index + 1) % HISTORY;
        self.filled = (self.filled + 1).min(HISTORY);
        if self.filled < HISTORY {
            return;
        }
        let mut sorted = self.history;
        sorted.sort_unstable_by(f32::total_cmp);
        self.average = sorted[..LOWER].iter().sum::<f32>() / LOWER as f32;
        self.features = self.features.saturating_add(1);
        match self.phase {
            Phase::Background => {
                if self.features == 1 {
                    self.lower = self.average;
                    self.upper = self.average;
                }
                self.lower = self.lower.min(self.average);
                self.upper = self.upper.max(self.average);
            }
            Phase::Holdout if self.average > self.enter => self.reject(Rejection::HoldoutExceeded),
            Phase::Ready => {
                if self.average > self.enter {
                    self.state = State::PresenceHint;
                } else if self.average <= self.exit {
                    self.state = State::NotDetected;
                }
            }
            _ => {}
        }
    }
    pub fn expire(&mut self, now: u64) {
        if !matches!(self.phase, Phase::Uncalibrated | Phase::Rejected) {
            if now < self.last_clock {
                self.reject(Rejection::ClockRollback);
            } else if now - self.last_received > self.config.freshness_us {
                self.reject(Rejection::ProgressExpired);
            }
            self.last_clock = now;
        }
    }
    pub fn snapshot(&mut self, now: u64) -> Snapshot {
        self.expire(now);
        self.checkpoint()
    }
    pub fn checkpoint(&self) -> Snapshot {
        Snapshot {
            phase: self.phase,
            state: self.state,
            rejection: self.rejection,
            features: self.features,
            average: self.average,
            lower: self.lower,
            upper: self.upper,
            enter: self.enter,
            exit: self.exit,
        }
    }
    pub fn diagnostic(&self, now: u64) -> History {
        History {
            phase: self.phase,
            rejection: self.rejection,
            features: self.features,
            filled: self.filled,
            index: self.index,
            average: self.average,
            lower: self.lower,
            upper: self.upper,
            enter: self.enter,
            exit: self.exit,
            values: self.history,
            sequences: self.history_sequences,
            background_ready: self.phase == Phase::Background && self.enough(now),
        }
    }
}

/// Callback paths never wait behind the lower-priority control task. Losing a
/// source update is sticky unavailable; a busy read loses no source evidence.
pub struct Runtime {
    enabled: AtomicBool,
    failure: AtomicU32,
    value: Mutex<Option<Calibration>>,
}
#[derive(Debug, PartialEq)]
pub enum ReadUnavailable {
    Busy,
    Disabled,
    Poisoned,
}
impl Default for Runtime {
    fn default() -> Self {
        Self::new()
    }
}
impl Runtime {
    pub const fn new() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            failure: AtomicU32::new(0),
            value: Mutex::new(None),
        }
    }
    pub fn prepare(&self, config: Config) -> bool {
        if self.enabled.load(Ordering::Acquire) {
            return false;
        }
        // Force lazy native mutex allocation on the control task.
        let Ok(mut value) = self.value.lock() else {
            return false;
        };
        let Some(calibration) = Calibration::new(config) else {
            return false;
        };
        *value = Some(calibration);
        self.enabled.store(true, Ordering::Release);
        true
    }
    pub fn fail(&self, reason: Rejection) {
        assert_ne!(reason, Rejection::None);
        if self.enabled.load(Ordering::Acquire) {
            let _ = self.failure.compare_exchange(
                0,
                reason as u32,
                Ordering::AcqRel,
                Ordering::Acquire,
            );
        }
    }
    fn apply_failure(&self, value: &mut Calibration) {
        let reason = Rejection::from_code(self.failure.load(Ordering::Acquire));
        if reason != Rejection::None {
            value.reject(reason);
        }
    }
    fn write<T>(
        &self,
        contention: Rejection,
        call: impl FnOnce(&mut Calibration) -> T,
    ) -> Option<T> {
        if !self.enabled.load(Ordering::Acquire) {
            return None;
        }
        let mut target = match self.value.try_lock() {
            Ok(value) => value,
            Err(TryLockError::WouldBlock) => {
                self.fail(contention);
                return None;
            }
            Err(TryLockError::Poisoned(_)) => {
                self.fail(Rejection::Poisoned);
                return None;
            }
        };
        let value = target.as_mut()?;
        self.apply_failure(value);
        Some(call(value))
    }
    pub fn update<T>(&self, call: impl FnOnce(&mut Calibration) -> T) -> Option<T> {
        self.write(Rejection::ControlContention, call)
    }
    pub fn observe(&self, feature: Feature, clock: impl FnOnce() -> u64) {
        self.observe_with(feature, clock, |_, _| {});
    }
    pub fn observe_with(
        &self,
        feature: Feature,
        clock: impl FnOnce() -> u64,
        after: impl FnOnce(&Calibration, u64),
    ) {
        self.write(Rejection::SourceContention, |value| {
            // Sample only after access: a preempted caller must not compare an
            // older clock sample against a newer source publication.
            let now = clock();
            value.observe(feature, now);
            after(value, now);
        });
    }
    /// Typed transaction used by live capture and replay. Recorder runs while
    /// ownership is held so events cannot be reordered by task preemption.
    pub fn execute(
        &self,
        op: crate::journal::Operation,
        clock: impl FnOnce() -> u64,
        record: impl FnOnce(crate::journal::Record),
    ) -> Option<(bool, Snapshot)> {
        self.execute_with(op, clock, record, |_, _| {})
    }
    pub fn execute_with(
        &self,
        op: crate::journal::Operation,
        clock: impl FnOnce() -> u64,
        record: impl FnOnce(crate::journal::Record),
        after_value: impl FnOnce(&Calibration, u64),
    ) -> Option<(bool, Snapshot)> {
        let call = |value: &mut Calibration| {
            let now = clock();
            let before = Rejection::from_code(self.failure.load(Ordering::Acquire));
            if before != Rejection::None {
                value.reject(before);
            }
            let accepted = op.apply(value, now);
            let after = Rejection::from_code(self.failure.load(Ordering::Acquire));
            if after != Rejection::None {
                value.reject(after);
            }
            let s = value.checkpoint();
            record(crate::journal::model(op, now, before, after, accepted, s));
            after_value(value, now);
            (accepted, s)
        };
        if matches!(op, crate::journal::Operation::Poll) {
            if !self.enabled.load(Ordering::Acquire) {
                return None;
            }
            let mut guard = self.value.try_lock().ok()?;
            Some(call(guard.as_mut()?))
        } else {
            let contention = if matches!(op, crate::journal::Operation::Observe(_)) {
                Rejection::SourceContention
            } else {
                Rejection::ControlContention
            };
            self.write(contention, call)
        }
    }
    pub fn snapshot(&self, clock: impl FnOnce() -> u64) -> Result<Snapshot, ReadUnavailable> {
        self.snapshot_with(clock, |_, _, _| {})
    }
    pub fn snapshot_with(
        &self,
        clock: impl FnOnce() -> u64,
        after: impl FnOnce(&Calibration, u64, Snapshot),
    ) -> Result<Snapshot, ReadUnavailable> {
        if !self.enabled.load(Ordering::Acquire) {
            return Err(ReadUnavailable::Disabled);
        }
        let mut target = match self.value.try_lock() {
            Ok(value) => value,
            Err(TryLockError::WouldBlock) => return Err(ReadUnavailable::Busy),
            Err(TryLockError::Poisoned(_)) => {
                self.fail(Rejection::Poisoned);
                return Err(ReadUnavailable::Poisoned);
            }
        };
        let value = target.as_mut().ok_or(ReadUnavailable::Disabled)?;
        self.apply_failure(value);
        let now = clock();
        let mut snapshot = value.snapshot(now);
        // A source/native fault may have arrived while the model was held.
        self.apply_failure(value);
        snapshot.phase = value.phase;
        snapshot.state = value.state;
        snapshot.rejection = value.rejection;
        after(value, now, snapshot);
        Ok(snapshot)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn model() -> Calibration {
        Calibration::new(Config {
            minimum_features: 25,
            minimum_duration_us: 1000,
            freshness_us: 1_000_000,
            ..Config::EMPTY_HOLDOUT
        })
        .unwrap()
    }
    fn feed(c: &mut Calibration, n: &mut u32, value: f32, count: u32) {
        for _ in 0..count {
            *n += 1;
            let us = u64::from(*n) * 1000;
            c.observe(
                Feature {
                    sequence: *n,
                    oldest_us: us,
                    newest_us: us,
                    templates: 10,
                    training_complete: true,
                    gain_available: true,
                    wander: value,
                },
                us,
            );
        }
    }
    fn accepted() -> (Calibration, u32) {
        let mut c = model();
        let mut n = 0;
        assert!(c.start(0, 10));
        feed(&mut c, &mut n, 1.0 / 1024.0, 60);
        assert!(c.validate(u64::from(n) * 1000));
        assert_eq!(c.features, 0);
        assert_eq!(c.filled, 0);
        feed(&mut c, &mut n, 1.0 / 1024.0, 60);
        assert!(c.accept(u64::from(n) * 1000));
        (c, n)
    }
    #[test]
    fn same_background_is_clear_at_every_sensitivity_not_positive_by_construction() {
        for sensitivity in [0.0, 0.25, 1.0] {
            let mut c = model();
            c.config.sensitivity = sensitivity;
            let mut n = 0;
            assert!(c.start(0, 10));
            feed(&mut c, &mut n, 1.0 / 1024.0, 60);
            assert!(c.validate(u64::from(n) * 1000));
            assert!(c.enter > 1.0 / 1024.0);
            feed(&mut c, &mut n, 1.0 / 1024.0, 60);
            assert!(c.accept(u64::from(n) * 1000));
            assert_eq!(c.state, State::Unknown);
            feed(&mut c, &mut n, 1.0 / 1024.0, 1);
            assert_eq!(c.state, State::NotDetected);
        }
    }
    #[test]
    fn history_never_counts_zero_padding_and_holdout_does_not_reuse_training() {
        let mut c = model();
        let mut n = 0;
        assert!(c.start(0, 10));
        feed(&mut c, &mut n, 0.1, 24);
        assert_eq!(c.features, 0);
        assert!(!c.validate(24000));
        feed(&mut c, &mut n, 0.1, 36);
        assert!(c.validate(60000));
        feed(&mut c, &mut n, 0.1, 24);
        assert_eq!(c.features, 0);
        assert!(!c.accept(84000));
    }
    #[test]
    fn drift_rejects_without_raising_threshold_or_relabeling_empty_as_clear() {
        let mut c = model();
        let mut n = 0;
        assert!(c.start(0, 10));
        feed(&mut c, &mut n, 0.001, 60);
        assert!(c.validate(60000));
        let threshold = c.enter;
        feed(&mut c, &mut n, 0.01, 60);
        assert_eq!(c.phase, Phase::Rejected);
        assert_eq!(c.state, State::Unknown);
        assert_eq!(c.enter, threshold);
        assert!(!c.accept(120000));
        assert!(!c.start(120000, 10));
    }
    #[test]
    fn positive_then_return_to_background_without_adapting_baseline() {
        let (mut c, mut n) = accepted();
        let threshold = c.enter;
        feed(&mut c, &mut n, 0.02, 30);
        assert_eq!(c.state, State::PresenceHint);
        feed(&mut c, &mut n, 1.0 / 1024.0, 30);
        assert_eq!(c.state, State::NotDetected);
        assert_eq!(c.enter, threshold);
    }
    #[test]
    fn missing_radio_progress_expires_even_without_more_callbacks_and_stays_rejected() {
        let (mut c, mut n) = accepted();
        feed(&mut c, &mut n, 1.0 / 1024.0, 1);
        assert_eq!(
            c.snapshot(u64::from(n) * 1000 + 1_000_001).state,
            State::Unknown
        );
        feed(&mut c, &mut n, 1.0 / 1024.0, 1);
        assert_eq!(c.phase, Phase::Rejected);
    }
    #[test]
    fn invalid_features_templates_gain_and_source_identity_reject() {
        for case in 0..8 {
            let (mut c, n) = accepted();
            let us = u64::from(n + 1) * 1000;
            let mut f = Feature {
                sequence: n + 1,
                oldest_us: us,
                newest_us: us,
                templates: 10,
                training_complete: true,
                gain_available: true,
                wander: 0.001,
            };
            match case {
                0 => f.wander = f32::NAN,
                1 => f.wander = 1.01,
                2 => f.templates = 11,
                3 => f.training_complete = false,
                4 => f.gain_available = false,
                5 => f.sequence = n,
                6 => f.newest_us -= 1000,
                7 => f.oldest_us = 0,
                _ => unreachable!(),
            }
            c.observe(f, if case == 7 { 1_200_000 } else { us });
            assert_eq!(c.phase, Phase::Rejected, "case {case}");
        }
    }
    #[test]
    fn queued_pre_phase_data_cannot_contribute_and_minimum_time_is_required() {
        let mut c = model();
        assert!(c.start(1000, 10));
        c.observe(
            Feature {
                sequence: 1,
                oldest_us: 999,
                newest_us: 1001,
                templates: 10,
                training_complete: true,
                gain_available: true,
                wander: 0.01,
            },
            1001,
        );
        assert_eq!(c.filled, 0);
        c.features = 100;
        c.lower = 0.01;
        c.upper = 0.01;
        assert!(!c.validate(1001));
    }
    #[test]
    fn saturated_background_and_bad_configuration_cannot_be_accepted() {
        let mut c = model();
        let mut n = 0;
        assert!(c.start(0, 10));
        feed(&mut c, &mut n, 1.0, 60);
        assert!(!c.validate(60000));
        assert_eq!(c.phase, Phase::Rejected);
        for sensitivity in [f32::NAN, -0.1, 1.1] {
            assert!(Calibration::new(Config {
                sensitivity,
                ..Config::EMPTY_HOLDOUT
            })
            .is_none());
        }
    }
    #[test]
    fn callback_contention_is_nonblocking_sticky_failure_and_prepare_cannot_reset_it() {
        let r = Runtime::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        let paused = r.value.lock().unwrap();
        r.observe(
            Feature {
                sequence: 1,
                oldest_us: 0,
                newest_us: 0,
                templates: 10,
                training_complete: true,
                gain_available: true,
                wander: 0.01,
            },
            || 0,
        );
        drop(paused);
        assert_eq!(
            r.failure.load(Ordering::Acquire),
            Rejection::SourceContention as u32
        );
        assert_eq!(r.update(|v| v.snapshot(0).phase), Some(Phase::Rejected));
        assert!(!r.prepare(Config::EMPTY_HOLDOUT));
    }
    #[test]
    fn exact_recorded_background_counterexample_is_not_positive_after_calibration() {
        let mut c = model();
        let mut n = 0;
        let baseline = 0.00088180;
        assert!(baseline * 8.25 > baseline); // Unchanged vendor default rule.
        assert!(c.start(0, 10));
        feed(&mut c, &mut n, baseline, 60);
        assert!(c.validate(u64::from(n) * 1000));
        feed(&mut c, &mut n, baseline, 60);
        assert!(c.accept(u64::from(n) * 1000));
        feed(&mut c, &mut n, baseline, 1);
        assert_eq!(c.state, State::NotDetected);
        // This is a constant-feature counterexample, NOT replay of all the
        // captured physical samples or a claim to fix RF feature drift.
    }
    #[test]
    fn radio_sequence_wrap_is_accepted_but_reversal_and_clock_rollback_are_not() {
        let (mut c, n) = accepted();
        c.last_source = Some((u32::MAX, u64::from(n) * 1000));
        let us = u64::from(n + 1) * 1000;
        c.observe(
            Feature {
                sequence: 0,
                oldest_us: us,
                newest_us: us,
                templates: 10,
                training_complete: true,
                gain_available: true,
                wander: 1.0 / 1024.0,
            },
            us,
        );
        assert_eq!(c.phase, Phase::Ready);
        c.observe(
            Feature {
                sequence: u32::MAX,
                oldest_us: us + 1000,
                newest_us: us + 1000,
                templates: 10,
                training_complete: true,
                gain_available: true,
                wander: 1.0 / 1024.0,
            },
            us + 1000,
        );
        assert_eq!(c.phase, Phase::Rejected);
        let (mut c, n) = accepted();
        c.expire(u64::from(n) * 1000 - 1);
        assert_eq!(c.phase, Phase::Rejected);
    }
    #[test]
    fn runtime_native_failure_invalidates_previously_accepted_result() {
        let r = Runtime::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        let (mut c, mut n) = accepted();
        feed(&mut c, &mut n, 1.0 / 1024.0, 1);
        *r.value.lock().unwrap() = Some(c);
        assert_eq!(
            r.snapshot(|| u64::from(n) * 1000).unwrap().state,
            State::NotDetected
        );
        r.fail(Rejection::NativeError);
        assert_eq!(
            r.snapshot(|| u64::from(n) * 1000).unwrap().state,
            State::Unknown
        );
        assert_eq!(
            r.snapshot(|| u64::from(n) * 1000).unwrap().phase,
            Phase::Rejected
        );
    }
    #[test]
    fn read_contention_is_unknown_without_sampling_clock_or_poisoning_model() {
        let r = Runtime::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        let (c, n) = accepted();
        *r.value.lock().unwrap() = Some(c);
        let paused = r.value.lock().unwrap();
        assert!(matches!(
            r.snapshot(|| panic!("clock sampled before access")),
            Err(ReadUnavailable::Busy)
        ));
        drop(paused);
        assert_eq!(
            r.snapshot(|| u64::from(n) * 1000).unwrap().phase,
            Phase::Ready
        );
        assert_eq!(r.failure.load(Ordering::Acquire), 0);
    }
    #[test]
    fn rejection_attribution_is_distinct_and_first_reason_is_sticky() {
        let (_, n) = accepted();
        let us = u64::from(n + 1) * 1000;
        let f = Feature {
            sequence: n + 1,
            oldest_us: us,
            newest_us: us,
            templates: 10,
            training_complete: true,
            gain_available: true,
            wander: 0.001,
        };
        for (case, expected) in [
            (0, Rejection::InvalidFeature),
            (1, Rejection::TrainingIncomplete),
            (2, Rejection::GainUnavailable),
            (3, Rejection::TemplatesChanged),
            (4, Rejection::InvalidRfTime),
            (5, Rejection::InvalidRfTime),
            (6, Rejection::RfExpired),
            (7, Rejection::SourceReversed),
        ] {
            let (mut c, _) = accepted();
            let mut f = f;
            match case {
                0 => f.wander = f32::NAN,
                1 => f.training_complete = false,
                2 => f.gain_available = false,
                3 => f.templates += 1,
                4 => f.oldest_us = f.newest_us + 1,
                5 => f.newest_us += 1,
                6 => {
                    f.oldest_us = 0;
                    c.config.freshness_us = us - 1;
                }
                7 => f.sequence -= 1,
                _ => unreachable!(),
            }
            c.observe(f, us);
            assert_eq!(c.rejection, expected, "case {case}");
            c.reject(Rejection::NativeError);
            assert_eq!(c.snapshot(us + 2_000_000).rejection, expected);
        }
    }
    #[test]
    fn true_rollback_after_poll_and_exact_freshness_boundary_remain_enforced() {
        let (mut c, n) = accepted();
        let now = u64::from(n) * 1000;
        assert_eq!(c.snapshot(now + 1_000_000).phase, Phase::Ready);
        assert_eq!(
            c.snapshot(now + 999_999).rejection,
            Rejection::ClockRollback
        );
        let (mut c, _) = accepted();
        assert_eq!(
            c.snapshot(now + 1_000_001).rejection,
            Rejection::ProgressExpired
        );
    }
    #[test]
    fn control_loss_and_native_fault_keep_first_pending_reason() {
        let r = Runtime::new();
        assert!(r.prepare(Config::EMPTY_HOLDOUT));
        let paused = r.value.lock().unwrap();
        assert_eq!(r.update(|_| ()), None);
        r.fail(Rejection::NativeError);
        drop(paused);
        assert_eq!(
            r.snapshot(|| 0).unwrap().rejection,
            Rejection::ControlContention
        );
    }
}

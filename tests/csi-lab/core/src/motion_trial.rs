//! One declared experiment schedule shared by device ownership and replay.
//! Labels are experiment evidence, not inputs to an admitted live detector.
use crate::{
    calibration::Feature,
    motion::{self, Admission, Bin, Fault, Motion, Phase, Snapshot},
    temporal::Sample,
};

#[derive(Clone, Copy)]
pub struct SourceSupport {
    pub oldest_us: u64,
    pub training_complete: bool,
    pub gain_available: bool,
}
impl From<Feature> for SourceSupport {
    fn from(f: Feature) -> Self {
        Self {
            oldest_us: f.oldest_us,
            training_complete: f.training_complete,
            gain_available: f.gain_available,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Label {
    Unknown,
    Empty,
    Walking,
    Seated,
}
impl Label {
    pub fn from_word(value: u32) -> Option<Self> {
        Some(match value {
            0 => Self::Unknown,
            1 => Self::Empty,
            2 => Self::Walking,
            3 => Self::Seated,
            _ => return None,
        })
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Empty => "empty",
            Self::Walking => "walking",
            Self::Seated => "seated",
        }
    }
}
#[derive(Clone, Copy)]
pub enum Event {
    Clock {
        time_us: u64,
        unavailable: Option<Fault>,
    },
    End(u64),
    Label {
        time_us: u64,
        label: Label,
    },
    TrainingStop {
        time_us: u64,
        success: bool,
    },
    Source {
        time_us: u64,
        sample: Option<Sample>,
        unavailable: Option<Fault>,
        support: SourceSupport,
    },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Source,
    Timer,
    End,
    Label,
    TrainingStop,
    BeginBackground,
    BeginHoldout,
    Accept,
    ControlRejected,
}
#[derive(Clone, Copy)]
pub struct Update {
    pub kind: Kind,
    pub time_us: u64,
    pub label: Label,
    pub bin: Option<Bin>,
    pub snapshot: Snapshot,
}
pub struct Trial {
    model: Motion,
    label: Label,
    label_since: u64,
    training_stopped: Option<u64>,
    accepted: bool,
}
impl Default for Trial {
    fn default() -> Self {
        Self::new()
    }
}
impl Trial {
    pub fn new() -> Self {
        Self {
            model: Motion::new(),
            label: Label::Unknown,
            label_since: 0,
            training_stopped: None,
            accepted: false,
        }
    }
    pub fn snapshot(&self) -> Snapshot {
        self.model.snapshot()
    }
    pub fn completed_holdout(&self) -> bool {
        self.accepted
    }
    fn update(&self, kind: Kind, time_us: u64, bin: Option<Bin>) -> Update {
        Update {
            kind,
            time_us,
            label: if bin.is_some_and(|bin| bin.oldest_us < self.label_since) {
                Label::Unknown
            } else {
                self.label
            },
            bin,
            snapshot: self.model.snapshot(),
        }
    }
    fn control(&mut self, now: u64, emit: &mut impl FnMut(Update)) {
        let prior = self.model.snapshot();
        let (kind, result) = match prior.phase {
            Phase::Uncalibrated
                if self.training_stopped.is_some() && self.label == Label::Empty =>
            {
                (Kind::BeginBackground, self.model.begin_background(now))
            }
            Phase::Background if prior.phase_bins >= motion::BACKGROUND_BINS as u32 => {
                (Kind::BeginHoldout, self.model.begin_holdout(now))
            }
            Phase::Holdout if prior.phase_bins >= motion::HOLDOUT_BINS => {
                (Kind::Accept, self.model.accept(now))
            }
            _ => return,
        };
        match result {
            Ok(()) => {
                self.accepted |= kind == Kind::Accept;
                emit(self.update(kind, now, None));
            }
            Err(Admission::NoSource | Admission::InsufficientEvidence) => {}
            Err(Admission::Rejected(_)) => emit(self.update(Kind::ControlRejected, now, None)),
            Err(Admission::WrongPhase) => {
                // Internal schedule inconsistency must never make clear evidence.
                self.model.reject(Fault::InvalidSample);
                emit(self.update(Kind::ControlRejected, now, None));
            }
        }
    }
    pub fn event(&mut self, event: Event, mut emit: impl FnMut(Update)) {
        match event {
            Event::Clock {
                time_us: now,
                unavailable,
            } => {
                let before = self.model.snapshot();
                let _ = self.model.poll(now);
                if let Some(fault) = unavailable {
                    self.model.reject(fault);
                }
                let after = self.model.snapshot();
                if (before.phase, before.state, before.fault)
                    != (after.phase, after.state, after.fault)
                {
                    emit(self.update(Kind::Timer, now, None));
                }
            }
            Event::End(now) => {
                let _ = self.model.poll(now);
                emit(self.update(Kind::End, now, None));
            }
            Event::Label { time_us, label } => {
                let _ = self.model.poll(time_us);
                self.label = label;
                self.label_since = time_us;
                if label != Label::Empty
                    && matches!(
                        self.model.snapshot().phase,
                        Phase::Background | Phase::Holdout
                    )
                {
                    self.model.reject(Fault::LabelViolation);
                }
                emit(self.update(Kind::Label, time_us, None));
            }
            Event::TrainingStop { time_us, success } => {
                let _ = self.model.poll(time_us);
                if success && self.training_stopped.is_none() {
                    self.training_stopped = Some(time_us);
                } else {
                    self.model.reject(Fault::SourceUnavailable);
                }
                emit(self.update(Kind::TrainingStop, time_us, None));
                self.control(time_us, &mut emit);
            }
            Event::Source {
                time_us,
                sample,
                unavailable,
                support,
            } => {
                // Windows wholly after the successful stop must carry valid
                // vendor readiness. In-flight pre-stop RF is already excluded
                // by the policy's source-support boundary; it is not a fault.
                let unsupported = self.training_stopped.is_some_and(|stop| {
                    support.oldest_us >= stop
                        && (!support.training_complete || !support.gain_available)
                });
                let unavailable = unavailable.or(unsupported.then_some(Fault::SourceUnavailable));
                let bin = if let Some(fault) = unavailable {
                    self.model.reject(fault);
                    None
                } else if let Some(sample) = sample {
                    let prior = self.model.snapshot().last_bin.map(|v| v.id);
                    let result = self.model.observe(sample, time_us);
                    let current = self.model.snapshot().last_bin;
                    match result {
                        Ok(bin) => bin,
                        Err(_) => current.filter(|v| Some(v.id) != prior),
                    }
                } else {
                    None
                };
                emit(self.update(Kind::Source, time_us, bin));
                self.control(time_us, &mut emit);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn readiness_guard_does_not_replace_the_original_signal_fault() {
        let mut t = Trial::new();
        t.event(
            Event::TrainingStop {
                time_us: 1,
                success: true,
            },
            |_| {},
        );
        t.event(
            Event::Source {
                time_us: 2,
                sample: None,
                unavailable: Some(Fault::Signal(crate::temporal::Fault::Degenerate)),
                support: SourceSupport {
                    oldest_us: 2,
                    training_complete: false,
                    gain_available: false,
                },
            },
            |_| {},
        );
        assert_eq!(
            t.snapshot().fault,
            Some(Fault::Signal(crate::temporal::Fault::Degenerate))
        );
    }
    #[test]
    fn post_stop_metadata_must_be_ready_and_pre_stop_rf_is_not_a_fault() {
        for (training_complete, gain_available) in [(false, true), (true, false), (false, false)] {
            let mut t = Trial::new();
            t.event(
                Event::Label {
                    time_us: 0,
                    label: Label::Empty,
                },
                |_| {},
            );
            t.event(
                Event::TrainingStop {
                    time_us: 10,
                    success: true,
                },
                |_| {},
            );
            t.event(
                Event::Source {
                    time_us: 11,
                    sample: None,
                    unavailable: None,
                    support: SourceSupport {
                        oldest_us: 9,
                        training_complete,
                        gain_available,
                    },
                },
                |_| {},
            );
            assert_eq!(t.snapshot().fault, None);
            t.event(
                Event::Source {
                    time_us: 12,
                    sample: None,
                    unavailable: None,
                    support: SourceSupport {
                        oldest_us: 10,
                        training_complete,
                        gain_available,
                    },
                },
                |_| {},
            );
            assert_eq!(t.snapshot().fault, Some(Fault::SourceUnavailable));
            assert_eq!(t.snapshot().state, motion::State::Unknown);
            t.event(
                Event::Source {
                    time_us: 13,
                    sample: Some(Sample {
                        sequence: 1,
                        window: 1,
                        oldest_us: 13,
                        newest_us: 13,
                        value: 0.001,
                    }),
                    unavailable: None,
                    support: SourceSupport {
                        oldest_us: 13,
                        training_complete: true,
                        gain_available: true,
                    },
                },
                |_| {},
            );
            assert_eq!(t.snapshot().fault, Some(Fault::SourceUnavailable));
        }
    }
    #[test]
    fn duplicate_stop_cannot_move_the_readiness_boundary_or_restart_a_trial() {
        let mut t = Trial::new();
        t.event(
            Event::TrainingStop {
                time_us: 1,
                success: true,
            },
            |_| {},
        );
        t.event(
            Event::TrainingStop {
                time_us: 2,
                success: true,
            },
            |_| {},
        );
        assert_eq!(t.snapshot().fault, Some(Fault::SourceUnavailable));
    }
    #[test]
    fn labels_never_substitute_for_source_and_calibration_requires_empty() {
        let mut trial = Trial::new();
        trial.event(
            Event::TrainingStop {
                time_us: 1,
                success: true,
            },
            |_| {},
        );
        trial.event(
            Event::Source {
                time_us: 2,
                sample: Some(Sample {
                    sequence: 1,
                    window: 1,
                    oldest_us: 2,
                    newest_us: 2,
                    value: 0.01,
                }),
                unavailable: None,
                support: SourceSupport {
                    oldest_us: 2,
                    training_complete: true,
                    gain_available: true,
                },
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().phase, Phase::Uncalibrated);
        trial.event(
            Event::Label {
                time_us: 3,
                label: Label::Empty,
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().phase, Phase::Uncalibrated);
        let mut kinds = Vec::new();
        trial.event(
            Event::Source {
                time_us: 4,
                sample: Some(Sample {
                    sequence: 2,
                    window: 2,
                    oldest_us: 4,
                    newest_us: 4,
                    value: 0.01,
                }),
                unavailable: None,
                support: SourceSupport {
                    oldest_us: 2,
                    training_complete: true,
                    gain_available: true,
                },
            },
            |u| kinds.push(u.kind),
        );
        assert_eq!(kinds, vec![Kind::Source, Kind::BeginBackground]);
        trial.event(
            Event::Label {
                time_us: 5,
                label: Label::Walking,
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().fault, Some(Fault::LabelViolation));
        assert_eq!(trial.snapshot().state, motion::State::Unknown);
    }
    #[test]
    fn marker_clock_rollback_and_failed_vendor_stop_are_sticky() {
        let mut trial = Trial::new();
        trial.event(
            Event::Label {
                time_us: 10,
                label: Label::Empty,
            },
            |_| {},
        );
        trial.event(
            Event::TrainingStop {
                time_us: 9,
                success: true,
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().fault, Some(Fault::ClockRollback));
        trial.event(
            Event::TrainingStop {
                time_us: 11,
                success: false,
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().fault, Some(Fault::ClockRollback));
        let mut trial = Trial::new();
        trial.event(
            Event::TrainingStop {
                time_us: 1,
                success: false,
            },
            |_| {},
        );
        assert_eq!(trial.snapshot().fault, Some(Fault::SourceUnavailable));
    }
}

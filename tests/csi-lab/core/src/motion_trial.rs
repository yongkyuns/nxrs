//! One declared experiment schedule shared by device ownership and replay.
//! Labels are experiment evidence, not inputs to an admitted live detector.
use crate::{
    motion::{self, Admission, Bin, Fault, Motion, Phase, Snapshot},
    temporal::Sample,
};

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
    training_stopped: bool,
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
            training_stopped: false,
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
            Phase::Uncalibrated if self.training_stopped && self.label == Label::Empty => {
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
                if success {
                    self.training_stopped = true;
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
            } => {
                let bin = if let Some(sample) = sample {
                    let prior = self.model.snapshot().last_bin.map(|v| v.id);
                    let result = self.model.observe(sample, time_us);
                    let current = self.model.snapshot().last_bin;
                    match result {
                        Ok(bin) => bin,
                        Err(_) => current.filter(|v| Some(v.id) != prior),
                    }
                } else {
                    if let Some(fault) = unavailable {
                        self.model.reject(fault);
                    }
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

//! Owned, bounded vector transfer and the original Rust motion experiment.
//! Native callbacks copy Profile; only the existing control owner runs Pipeline.
use crate::{
    calibration::Feature,
    motion::{Fault, FRESHNESS_US},
    motion_trial::{Event, Trial, Update},
    temporal::{Input, Temporal},
};

// Named LLTF/step-four experiment: the qualified source currently has 13 columns.
// Reject unsupported shapes rather than truncating or extending a native vector.
pub const COLUMNS: usize = 16;
#[derive(Clone, Copy)]
pub struct Profile {
    values: [f32; COLUMNS],
    length: usize,
}
impl Profile {
    pub fn copy(values: &[f32]) -> Option<Self> {
        if !(2..=COLUMNS).contains(&values.len()) {
            return None;
        }
        let mut profile = Self {
            values: [0.0; COLUMNS],
            length: values.len(),
        };
        profile.values[..values.len()].copy_from_slice(values);
        Some(profile)
    }
    pub fn values(&self) -> &[f32] {
        &self.values[..self.length]
    }
}
pub struct Pipeline {
    signal: Temporal,
    trial: Trial,
}
impl Default for Pipeline {
    fn default() -> Self {
        Self::new()
    }
}
impl Pipeline {
    pub fn new() -> Self {
        Self {
            signal: Temporal::new(FRESHNESS_US).unwrap(),
            trial: Trial::new(),
        }
    }
    pub fn event(&mut self, event: Event, emit: impl FnMut(Update)) {
        self.trial.event(event, emit);
    }
    pub fn snapshot(&self) -> crate::motion::Snapshot {
        self.trial.snapshot()
    }
    pub fn clock(&mut self, now: u64, unavailable: bool, emit: impl FnMut(Update)) {
        let freshness = self.signal.check_fresh(now);
        let fault = if unavailable {
            Some(Fault::SourceUnavailable)
        } else {
            freshness.err().map(Fault::Signal)
        };
        self.trial.event(
            Event::Clock {
                time_us: now,
                unavailable: fault,
            },
            emit,
        );
    }
    pub fn source(
        &mut self,
        feature: Feature,
        window: u32,
        profile: Option<Profile>,
        now: u64,
        unavailable: bool,
        emit: impl FnMut(Update),
    ) {
        let result = if unavailable || (feature.training_complete && !feature.gain_available) {
            Err(Fault::SourceUnavailable)
        } else if let Some(profile) = profile {
            self.signal
                .observe(
                    Input {
                        sequence: feature.sequence,
                        window,
                        oldest_us: feature.oldest_us,
                        newest_us: feature.newest_us,
                        values: profile.values(),
                    },
                    now,
                )
                .map_err(Fault::Signal)
        } else {
            Err(Fault::SourceUnavailable)
        };
        let (sample, unavailable) = match result {
            Ok(sample) => (sample, None),
            Err(fault) => (None, Some(fault)),
        };
        self.trial.event(
            Event::Source {
                time_us: now,
                sample,
                unavailable,
            },
            emit,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn vector_copy_is_owned_bounded_and_invalid_math_is_unavailable() {
        assert!(Profile::copy(&[1.0]).is_none());
        assert!(Profile::copy(&[1.0; 17]).is_none());
        let mut values = [1.0, 2.0, 3.0, 4.0];
        let profile = Profile::copy(&values).unwrap();
        values.fill(f32::NAN);
        assert_eq!(profile.values(), &[1.0, 2.0, 3.0, 4.0]);
        let mut model = Pipeline::new();
        let feature = Feature {
            sequence: 1,
            oldest_us: 1,
            newest_us: 1,
            templates: 0,
            training_complete: false,
            gain_available: true,
            wander: 0.0,
        };
        model.source(feature, 1, Profile::copy(&values), 1, false, |_| {});
        assert_eq!(
            model.snapshot().fault,
            Some(Fault::Signal(crate::temporal::Fault::Nonfinite))
        );
        model.source(
            Feature {
                sequence: 2,
                newest_us: 2,
                ..feature
            },
            2,
            Some(profile),
            2,
            false,
            |_| {},
        );
        assert_eq!(model.snapshot().state, crate::motion::State::Unknown);
        assert!(std::mem::size_of::<Pipeline>() <= 4096);
        assert!(crate::ownership::Ingress::storage_bytes(64) <= 16384);
    }
}

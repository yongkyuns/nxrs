//! Test-application checkpoint contract, independent of any native SDK layout.
use crate::{
    motion::{Fault, Phase, State},
    motion_trial::{Kind, Update},
    temporal,
};
pub const WORDS: usize = 32;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Checkpoint {
    pub time_us: u64,
    pub words: [u32; WORDS],
}
impl From<Update> for Checkpoint {
    fn from(u: Update) -> Self {
        let mut w = [0; WORDS];
        w[0] = 1;
        w[1] = match u.kind {
            Kind::Source => 1,
            Kind::Timer => 2,
            Kind::End => 3,
            Kind::Label => 4,
            Kind::TrainingStop => 5,
            Kind::BeginBackground => 6,
            Kind::BeginHoldout => 7,
            Kind::Accept => 8,
            Kind::ControlRejected => 9,
        };
        let s = u.snapshot;
        w[2] = match s.phase {
            Phase::Uncalibrated => 0,
            Phase::Background => 1,
            Phase::Holdout => 2,
            Phase::Ready => 3,
            Phase::Rejected => 4,
        };
        w[3] = match s.state {
            State::Unknown => 0,
            State::Idle => 1,
            State::Moving => 2,
        };
        (w[4], w[5]) = fault(s.fault);
        w[6] = u.label as u32;
        w[7] = s.phase_bins;
        w[8] = s.positive_bins;
        w[9] = s.quiet_bins;
        if let Some(t) = s.thresholds {
            w[10] = 1;
            w[11] = t.centre.to_bits();
            w[12] = t.mad.to_bits();
            w[13] = t.enter.to_bits();
            w[14] = t.exit.to_bits();
        }
        if let Some(b) = u.bin {
            w[15] = 1;
            w[16] = b.id as u32;
            w[17] = (b.id >> 32) as u32;
            w[18] = b.oldest_us as u32;
            w[19] = (b.oldest_us >> 32) as u32;
            w[20] = b.newest_us as u32;
            w[21] = (b.newest_us >> 32) as u32;
            w[22] = b.sequence;
            w[23] = b.window;
            w[24] = b.samples;
            w[25] = b.score.to_bits();
        }
        Self {
            time_us: u.time_us,
            words: w,
        }
    }
}
fn fault(f: Option<Fault>) -> (u32, u32) {
    (
        match f {
            None => 0,
            Some(Fault::Signal(_)) => 1,
            Some(Fault::InvalidSample) => 2,
            Some(Fault::SourceReversed) => 3,
            Some(Fault::ClockRollback) => 4,
            Some(Fault::Expired) => 5,
            Some(Fault::Overload) => 6,
            Some(Fault::BinGap) => 7,
            Some(Fault::SparseBin) => 8,
            Some(Fault::SaturatedBackground) => 9,
            Some(Fault::HoldoutMotion) => 10,
            Some(Fault::SourceUnavailable) => 11,
            Some(Fault::LabelViolation) => 12,
        },
        match f {
            Some(Fault::Signal(f)) => match f {
                temporal::Fault::Shape => 1,
                temporal::Fault::Nonfinite => 2,
                temporal::Fault::Degenerate => 3,
                temporal::Fault::SourceReversed => 4,
                temporal::Fault::InvalidTime => 5,
                temporal::Fault::Expired => 6,
                temporal::Fault::ClockRollback => 7,
            },
            _ => 0,
        },
    )
}
pub fn validate(words: &[u32]) -> Result<(), &'static str> {
    if words.len() != WORDS
        || words[0] != 1
        || !(1..=9).contains(&words[1])
        || words[2] > 4
        || words[3] > 2
        || words[4] > 12
        || words[5] > 7
        || (words[4] == 1) != (words[5] != 0)
        || words[6] > 3
        || words[10] > 1
        || words[15] > 1
        || words[26..].iter().any(|v| *v != 0)
        || (words[10] == 0 && words[11..15] != [0; 4])
        || (words[15] == 0 && words[16..26] != [0; 10])
    {
        return Err("motion checkpoint schema");
    }
    Ok(())
}

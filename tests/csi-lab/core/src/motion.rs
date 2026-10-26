//! Declared experimental temporal-motion policy. This is motion evidence, not
//! stationary-person occupancy. No production detector is admitted by this core.
use crate::temporal::{self, Sample};

pub const BIN_US: u64 = 250_000;
pub const BACKGROUND_BINS: usize = 128;
pub const HOLDOUT_BINS: u32 = 240;
pub const FRESHNESS_US: u64 = 1_000_000;
const MIN_BIN_SAMPLES: usize = 3;
const MAX_BIN_SAMPLES: usize = 64;
const MIN_BACKGROUND_US: u64 = 32_000_000;
const MIN_HOLDOUT_US: u64 = 60_000_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Uncalibrated,
    Background,
    Holdout,
    Ready,
    Rejected,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Unknown,
    Idle,
    Moving,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    Signal(temporal::Fault),
    InvalidSample,
    SourceReversed,
    ClockRollback,
    Expired,
    Overload,
    BinGap,
    SparseBin,
    SaturatedBackground,
    HoldoutMotion,
    SourceUnavailable,
    LabelViolation,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admission {
    WrongPhase,
    NoSource,
    InsufficientEvidence,
    Rejected(Fault),
}
#[derive(Clone, Copy, Debug)]
pub struct Thresholds {
    pub centre: f32,
    pub mad: f32,
    pub enter: f32,
    pub exit: f32,
}
#[derive(Clone, Copy, Debug)]
pub struct Bin {
    pub id: u64,
    pub oldest_us: u64,
    pub newest_us: u64,
    pub sequence: u32,
    pub window: u32,
    pub samples: u32,
    pub score: f32,
}
#[derive(Clone, Copy, Debug)]
pub struct Snapshot {
    pub phase: Phase,
    pub state: State,
    pub fault: Option<Fault>,
    pub phase_bins: u32,
    pub positive_bins: u32,
    pub quiet_bins: u32,
    pub thresholds: Option<Thresholds>,
    pub last_bin: Option<Bin>,
}
struct Pending {
    id: u64,
    values: [f32; MAX_BIN_SAMPLES],
    count: usize,
    oldest_us: u64,
    newest_us: u64,
    sequence: u32,
    window: u32,
}
impl Pending {
    fn new(sample: Sample) -> Self {
        let mut values = [0.0; MAX_BIN_SAMPLES];
        values[0] = sample.value;
        Self {
            id: sample.newest_us / BIN_US,
            values,
            count: 1,
            oldest_us: sample.oldest_us,
            newest_us: sample.newest_us,
            sequence: sample.sequence,
            window: sample.window,
        }
    }
    fn append(&mut self, sample: Sample) -> Result<(), Fault> {
        if self.count == MAX_BIN_SAMPLES {
            return Err(Fault::Overload);
        }
        self.values[self.count] = sample.value;
        self.count += 1;
        self.oldest_us = self.oldest_us.min(sample.oldest_us);
        self.newest_us = sample.newest_us;
        self.sequence = sample.sequence;
        self.window = sample.window;
        Ok(())
    }
    fn finish(mut self) -> Result<Bin, Fault> {
        if self.count < MIN_BIN_SAMPLES {
            return Err(Fault::SparseBin);
        }
        let score = median(&mut self.values[..self.count]);
        Ok(Bin {
            id: self.id,
            oldest_us: self.oldest_us,
            newest_us: self.newest_us,
            sequence: self.sequence,
            window: self.window,
            samples: self.count as u32,
            score,
        })
    }
}
fn median(values: &mut [f32]) -> f32 {
    values.sort_unstable_by(f32::total_cmp);
    let n = values.len();
    if n.is_multiple_of(2) {
        (values[n / 2 - 1] + values[n / 2]) * 0.5
    } else {
        values[n / 2]
    }
}

pub struct Motion {
    phase: Phase,
    state: State,
    fault: Option<Fault>,
    phase_started: u64,
    last_clock: u64,
    last_source: Option<(u32, u32, u64)>,
    pending: Option<Pending>,
    discard_first: bool,
    background: [f32; BACKGROUND_BINS],
    background_index: usize,
    phase_bins: u32,
    positive_bins: u32,
    quiet_bins: u32,
    on_hits: u32,
    off_hits: u32,
    confirmation: State,
    thresholds: Option<Thresholds>,
    last_bin: Option<Bin>,
}
impl Default for Motion {
    fn default() -> Self {
        Self::new()
    }
}
impl Motion {
    pub fn new() -> Self {
        Self {
            phase: Phase::Uncalibrated,
            state: State::Unknown,
            fault: None,
            phase_started: 0,
            last_clock: 0,
            last_source: None,
            pending: None,
            discard_first: true,
            background: [0.0; BACKGROUND_BINS],
            background_index: 0,
            phase_bins: 0,
            positive_bins: 0,
            quiet_bins: 0,
            on_hits: 0,
            off_hits: 0,
            confirmation: State::Unknown,
            thresholds: None,
            last_bin: None,
        }
    }
    pub fn reject(&mut self, reason: Fault) {
        if self.fault.is_none() {
            self.fault = Some(reason);
        }
        self.phase = Phase::Rejected;
        self.state = State::Unknown;
        self.confirmation = State::Unknown;
    }
    fn fail<T>(&mut self, reason: Fault) -> Result<T, Fault> {
        self.reject(reason);
        Err(self.fault.unwrap())
    }
    pub fn poll(&mut self, now_us: u64) -> Result<Snapshot, Fault> {
        if let Some(fault) = self.fault {
            return Err(fault);
        }
        if now_us < self.last_clock {
            return self.fail(Fault::ClockRollback);
        }
        self.last_clock = now_us;
        if let Some((_, _, newest)) = self.last_source {
            if now_us - newest > FRESHNESS_US {
                return self.fail(Fault::Expired);
            }
        }
        Ok(self.snapshot())
    }
    pub fn snapshot(&self) -> Snapshot {
        Snapshot {
            phase: self.phase,
            state: self.state,
            fault: self.fault,
            phase_bins: self.phase_bins,
            positive_bins: self.positive_bins,
            quiet_bins: self.quiet_bins,
            thresholds: self.thresholds,
            last_bin: self.last_bin,
        }
    }
    fn boundary(&mut self, phase: Phase, now: u64) {
        self.phase = phase;
        self.phase_started = now;
        self.pending = None;
        self.discard_first = true;
        self.phase_bins = 0;
        self.positive_bins = 0;
        self.quiet_bins = 0;
        self.on_hits = 0;
        self.off_hits = 0;
        self.confirmation = State::Unknown;
        self.state = State::Unknown;
        self.last_bin = None;
    }
    fn admit(&mut self, now: u64, phase: Phase) -> Result<(), Admission> {
        self.poll(now).map_err(Admission::Rejected)?;
        if self.phase != phase {
            return Err(Admission::WrongPhase);
        }
        if self.last_source.is_none() {
            return Err(Admission::NoSource);
        }
        Ok(())
    }
    /// Explicit empty-background command; room labels belong to the caller.
    pub fn begin_background(&mut self, now: u64) -> Result<(), Admission> {
        self.admit(now, Phase::Uncalibrated)?;
        self.background.fill(0.0);
        self.background_index = 0;
        self.boundary(Phase::Background, now);
        Ok(())
    }
    pub fn begin_holdout(&mut self, now: u64) -> Result<(), Admission> {
        self.admit(now, Phase::Background)?;
        if self.phase_bins < BACKGROUND_BINS as u32 || now - self.phase_started < MIN_BACKGROUND_US
        {
            return Err(Admission::InsufficientEvidence);
        }
        let mut samples = self.background;
        let centre = median(&mut samples);
        for value in &mut samples {
            *value = (*value - centre).abs();
        }
        let mad = median(&mut samples);
        let dispersion = mad.max(0.000001);
        let enter = centre + 6.0 * dispersion;
        let exit = centre + 3.0 * dispersion;
        if !enter.is_finite() || enter >= 1.0 || exit >= enter {
            self.reject(Fault::SaturatedBackground);
            return Err(Admission::Rejected(Fault::SaturatedBackground));
        }
        self.thresholds = Some(Thresholds {
            centre,
            mad,
            enter,
            exit,
        });
        self.boundary(Phase::Holdout, now);
        Ok(())
    }
    pub fn accept(&mut self, now: u64) -> Result<(), Admission> {
        self.admit(now, Phase::Holdout)?;
        if self.phase_bins < HOLDOUT_BINS
            || now - self.phase_started < MIN_HOLDOUT_US
            || self.confirmation != State::Idle
        {
            return Err(Admission::InsufficientEvidence);
        }
        // No held-out samples or earlier quiet state can initialize live state.
        self.boundary(Phase::Ready, now);
        Ok(())
    }
    fn complete(&mut self, bin: Bin) -> Result<(), Fault> {
        self.last_bin = Some(bin);
        self.phase_bins = self.phase_bins.saturating_add(1);
        if self.phase == Phase::Background {
            self.background[self.background_index] = bin.score;
            self.background_index = (self.background_index + 1) % BACKGROUND_BINS;
            return Ok(());
        }
        let thresholds = self.thresholds.ok_or(Fault::InvalidSample)?;
        if bin.score > thresholds.enter {
            self.positive_bins = self.positive_bins.saturating_add(1);
            self.on_hits = self.on_hits.saturating_add(1);
            self.off_hits = 0;
        } else if bin.score <= thresholds.exit {
            self.quiet_bins = self.quiet_bins.saturating_add(1);
            self.off_hits = self.off_hits.saturating_add(1);
            self.on_hits = 0;
        } else {
            self.on_hits = 0;
            self.off_hits = 0;
        }
        if self.on_hits >= 4 {
            self.confirmation = State::Moving;
        } else if self.off_hits >= 3 {
            self.confirmation = State::Idle;
        }
        if self.phase == Phase::Holdout && self.confirmation == State::Moving {
            return self.fail(Fault::HoldoutMotion);
        }
        if self.phase == Phase::Ready {
            self.state = self.confirmation;
        }
        Ok(())
    }
    pub fn observe(&mut self, sample: Sample, now: u64) -> Result<Option<Bin>, Fault> {
        self.poll(now)?;
        if !sample.value.is_finite() || !(0.0..=1.0).contains(&sample.value) || sample.window == 0 {
            return self.fail(Fault::InvalidSample);
        }
        if sample.oldest_us > sample.newest_us || sample.newest_us > now {
            return self.fail(Fault::InvalidSample);
        }
        if now - sample.oldest_us > FRESHNESS_US {
            return self.fail(Fault::Expired);
        }
        if let Some((sequence, window, newest)) = self.last_source {
            let delta = sample.sequence.wrapping_sub(sequence);
            let native = sample.window.wrapping_sub(window);
            if delta == 0
                || delta >= 1 << 31
                || native == 0
                || native >= 1 << 31
                || sample.newest_us <= newest
            {
                return self.fail(Fault::SourceReversed);
            }
        }
        self.last_source = Some((sample.sequence, sample.window, sample.newest_us));
        if self.phase == Phase::Uncalibrated || sample.oldest_us < self.phase_started {
            return Ok(None);
        }
        let Some(mut pending) = self.pending.take() else {
            self.pending = Some(Pending::new(sample));
            return Ok(None);
        };
        let id = sample.newest_us / BIN_US;
        if id == pending.id {
            if let Err(fault) = pending.append(sample) {
                return self.fail(fault);
            }
            self.pending = Some(pending);
            return Ok(None);
        }
        if id != pending.id + 1 {
            return self.fail(Fault::BinGap);
        }
        self.pending = Some(Pending::new(sample));
        if self.discard_first {
            self.discard_first = false;
            return Ok(None);
        }
        let bin = match pending.finish() {
            Ok(bin) => bin,
            Err(fault) => return self.fail(fault),
        };
        self.complete(bin)?;
        Ok(Some(bin))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Feed {
        model: Motion,
        sequence: u32,
        bin: u64,
        now: u64,
    }
    impl Feed {
        fn new() -> Self {
            let mut f = Self {
                model: Motion::new(),
                sequence: 0,
                bin: 0,
                now: 0,
            };
            f.at(1, 0.001).unwrap();
            f.model.begin_background(2).unwrap();
            f.now = 2;
            f
        }
        fn at(&mut self, newest: u64, value: f32) -> Result<Option<Bin>, Fault> {
            self.sequence += 1;
            self.now = newest;
            self.model.observe(
                Sample {
                    sequence: self.sequence,
                    window: self.sequence,
                    oldest_us: newest.saturating_sub(1000),
                    newest_us: newest,
                    value,
                },
                newest,
            )
        }
        fn bin(&mut self, value: f32) -> Result<(), Fault> {
            let id = self.bin;
            self.bin += 1;
            for offset in [20_000, 80_000, 140_000, 200_000] {
                self.at(id * BIN_US + offset, value)?;
            }
            Ok(())
        }
        fn background() -> Self {
            let mut f = Self::new();
            for n in 0..130 {
                f.bin(0.001 + (n % 5) as f32 * 0.0001).unwrap();
            }
            assert_eq!(f.model.snapshot().phase_bins, 128);
            f
        }
        fn holdout() -> Self {
            let mut f = Self::background();
            f.model.begin_holdout(f.now).unwrap();
            f
        }
        fn ready() -> Self {
            let mut f = Self::holdout();
            for _ in 0..242 {
                f.bin(0.001).unwrap();
            }
            assert_eq!(f.model.snapshot().phase_bins, 240);
            f.model.accept(f.now).unwrap();
            f
        }
    }
    #[test]
    fn independent_phases_and_new_live_evidence_are_required() {
        let mut f = Feed::new();
        assert_eq!(
            f.model.begin_holdout(f.now),
            Err(Admission::InsufficientEvidence)
        );
        assert_eq!(f.model.snapshot().phase, Phase::Background);
        let mut f = Feed::holdout();
        assert_eq!(f.model.snapshot().phase_bins, 0);
        assert_eq!(f.model.accept(f.now), Err(Admission::InsufficientEvidence));
        for _ in 0..241 {
            f.bin(0.001).unwrap();
        }
        assert_eq!(f.model.snapshot().phase_bins, 239);
        assert_eq!(f.model.accept(f.now), Err(Admission::InsufficientEvidence));
        f.bin(0.001).unwrap();
        f.model.accept(f.now).unwrap();
        assert_eq!(f.model.snapshot().state, State::Unknown);
        for _ in 0..4 {
            f.bin(0.001).unwrap();
            assert_eq!(f.model.snapshot().state, State::Unknown);
        }
        f.bin(0.001).unwrap();
        assert_eq!(f.model.snapshot().state, State::Idle);
    }
    #[test]
    fn isolated_peaks_pass_no_movement_and_sustained_evidence_fails_holdout() {
        let mut f = Feed::holdout();
        for n in 0..30 {
            f.bin(if n == 10 { 0.2 } else { 0.001 }).unwrap();
        }
        assert_eq!(f.model.snapshot().phase, Phase::Holdout);
        assert_eq!(f.model.snapshot().state, State::Unknown);
        assert_eq!(f.model.snapshot().positive_bins, 1);
        for _ in 0..4 {
            f.bin(0.2).unwrap();
        }
        assert_eq!(f.bin(0.2), Err(Fault::HoldoutMotion));
        assert_eq!(f.model.snapshot().state, State::Unknown);
        assert_eq!(
            f.model.accept(f.now),
            Err(Admission::Rejected(Fault::HoldoutMotion))
        );
    }
    #[test]
    fn sustained_movement_and_return_change_state_without_adapting_thresholds() {
        let mut f = Feed::ready();
        let thresholds = f.model.snapshot().thresholds.unwrap();
        for _ in 0..6 {
            f.bin(0.001).unwrap();
        }
        assert_eq!(f.model.snapshot().state, State::Idle);
        for _ in 0..5 {
            f.bin(0.2).unwrap();
        }
        assert_eq!(f.model.snapshot().state, State::Moving);
        for _ in 0..200 {
            f.bin(0.2).unwrap();
        }
        let after = f.model.snapshot().thresholds.unwrap();
        assert_eq!(thresholds.enter.to_bits(), after.enter.to_bits());
        assert_eq!(thresholds.exit.to_bits(), after.exit.to_bits());
        assert_eq!(thresholds.centre.to_bits(), after.centre.to_bits());
        for _ in 0..4 {
            f.bin(0.001).unwrap();
        }
        assert_eq!(f.model.snapshot().state, State::Idle);
    }
    #[test]
    fn a_non_extending_streak_and_uncertain_bins_cannot_confirm_movement() {
        let mut f = Feed::ready();
        for _ in 0..5 {
            f.bin(0.001).unwrap();
        }
        let t = f.model.snapshot().thresholds.unwrap();
        let middle = (t.enter + t.exit) * 0.5;
        for _ in 0..3 {
            f.bin(0.2).unwrap();
        }
        f.bin(middle).unwrap();
        for _ in 0..3 {
            f.bin(0.2).unwrap();
        }
        f.bin(middle).unwrap();
        f.bin(middle).unwrap();
        assert_eq!(f.model.snapshot().state, State::Idle);
    }
    #[test]
    fn source_support_cannot_straddle_calibration_boundaries() {
        let mut f = Feed::holdout();
        let boundary = f.now;
        for n in 1..=4 {
            f.sequence += 1;
            let newest = boundary + n * 50_000;
            f.model
                .observe(
                    Sample {
                        sequence: f.sequence,
                        window: f.sequence,
                        oldest_us: boundary - 1,
                        newest_us: newest,
                        value: 0.9,
                    },
                    newest,
                )
                .unwrap();
        }
        assert_eq!(f.model.snapshot().phase_bins, 0);
        assert!(f.model.snapshot().last_bin.is_none());
    }
    #[test]
    fn bin_median_requires_real_samples_and_keeps_provenance() {
        let mut f = Feed::new();
        f.bin(0.001).unwrap();
        let id = f.bin;
        for (offset, value) in [
            (20_000, 0.001),
            (80_000, 0.002),
            (140_000, 0.003),
            (200_000, 0.9),
        ] {
            f.at(id * BIN_US + offset, value).unwrap();
        }
        let bin = f.at((id + 1) * BIN_US + 20_000, 0.001).unwrap().unwrap();
        assert_eq!(bin.samples, 4);
        assert_eq!(bin.score, (0.002 + 0.003) * 0.5);
        assert_eq!(bin.id, id);
        assert_eq!(bin.oldest_us, id * BIN_US + 19_000);
        assert_eq!(bin.newest_us, id * BIN_US + 200_000);
        assert_eq!(bin.sequence, f.sequence - 1);
        assert_eq!(bin.window, bin.sequence);
    }
    #[test]
    fn overload_gaps_and_sparse_bins_are_fatal_without_padding_evidence() {
        let mut f = Feed::new();
        for t in 1100..1164 {
            f.at(t, 0.001).unwrap();
        }
        assert_eq!(f.at(1164, 0.001).unwrap_err(), Fault::Overload);
        let mut f = Feed::new();
        f.bin(0.001).unwrap();
        assert_eq!(f.at(2 * BIN_US + 20_000, 0.001).unwrap_err(), Fault::BinGap);
        let mut f = Feed::new();
        f.bin(0.001).unwrap();
        f.at(BIN_US + 20_000, 0.001).unwrap();
        assert_eq!(
            f.at(2 * BIN_US + 20_000, 0.001).unwrap_err(),
            Fault::SparseBin
        );
    }
    #[test]
    fn silence_and_external_failure_clear_state_and_never_recover_implicitly() {
        let mut f = Feed::ready();
        for _ in 0..6 {
            f.bin(0.2).unwrap();
        }
        assert_eq!(f.model.snapshot().state, State::Moving);
        assert_eq!(
            f.model.poll(f.now + FRESHNESS_US + 1).unwrap_err(),
            Fault::Expired
        );
        assert_eq!(f.model.snapshot().state, State::Unknown);
        assert_eq!(
            f.at(f.now + FRESHNESS_US + 2, 0.001).unwrap_err(),
            Fault::Expired
        );
        let mut f = Feed::ready();
        f.model.reject(Fault::SourceUnavailable);
        f.model.reject(Fault::LabelViolation);
        assert_eq!(f.model.poll(0).unwrap_err(), Fault::SourceUnavailable);
    }
    #[test]
    fn invalid_time_numeric_support_and_source_identity_are_sticky() {
        for reason in 0..5 {
            let mut f = Feed::new();
            let mut s = Sample {
                sequence: 2,
                window: 2,
                oldest_us: 2,
                newest_us: 10,
                value: 0.001,
            };
            let expected = match reason {
                0 => {
                    s.value = f32::NAN;
                    Fault::InvalidSample
                }
                1 => {
                    s.newest_us = 100;
                    Fault::InvalidSample
                }
                2 => {
                    s.sequence = 1;
                    Fault::SourceReversed
                }
                3 => {
                    s.window = 1;
                    Fault::SourceReversed
                }
                _ => {
                    s.oldest_us = 11;
                    Fault::InvalidSample
                }
            };
            assert_eq!(f.model.observe(s, 10).unwrap_err(), expected);
            assert_eq!(f.model.poll(1_000_000).unwrap_err(), expected);
        }
        let mut f = Feed::new();
        f.model.poll(100).unwrap();
        assert_eq!(f.model.poll(99).unwrap_err(), Fault::ClockRollback);
    }
    #[test]
    fn robust_background_rejects_saturation_and_controls_require_a_source() {
        let mut m = Motion::new();
        assert_eq!(m.begin_background(0), Err(Admission::NoSource));
        let mut f = Feed::new();
        for _ in 0..130 {
            f.bin(0.999999).unwrap();
        }
        assert_eq!(
            f.model.begin_holdout(f.now),
            Err(Admission::Rejected(Fault::SaturatedBackground))
        );
        let mut f = Feed::background();
        let expected = f.model.background;
        f.model.begin_holdout(f.now).unwrap();
        let t = f.model.snapshot().thresholds.unwrap();
        assert!(t.mad > 0.0 && t.enter > t.exit);
        assert_eq!(f.model.background, expected);
    }

    #[test]
    fn owned_vector_pipeline_distinguishes_a_permanent_step_from_continuous_motion() {
        struct Pipeline {
            feature: temporal::Temporal,
            model: Motion,
            sequence: u32,
            time: u64,
        }
        impl Pipeline {
            fn tick(&mut self, values: &[f32]) {
                self.sequence += 1;
                self.time += 25_000;
                let now = self.time + 1000;
                let sample = self
                    .feature
                    .observe(
                        temporal::Input {
                            sequence: self.sequence,
                            window: self.sequence,
                            oldest_us: self.time - 5000,
                            newest_us: self.time,
                            values,
                        },
                        now,
                    )
                    .unwrap();
                if let Some(sample) = sample {
                    self.model.observe(sample, now).unwrap();
                } else {
                    self.model.poll(now).unwrap();
                }
            }
        }
        let a = [1.0, 2.0, 3.0, 4.0];
        let b = [1.0, 4.0, 2.0, 3.0];
        let mut p = Pipeline {
            feature: temporal::Temporal::new(FRESHNESS_US).unwrap(),
            model: Motion::new(),
            sequence: 0,
            time: 0,
        };
        for _ in 0..10 {
            p.tick(&a);
        }
        p.model.begin_background(p.time + 1000).unwrap();
        while p.model.snapshot().phase_bins < BACKGROUND_BINS as u32 {
            p.tick(&a);
        }
        p.model.begin_holdout(p.time + 1000).unwrap();
        let started = p.time;
        while p.model.snapshot().phase_bins < HOLDOUT_BINS {
            // This is a synthetic change of channel profile in the independently
            // timed holdout, not a relabelled or retuned physical recording.
            let vector = if p.time - started < 15_000_000 {
                &a
            } else {
                &b
            };
            p.tick(vector);
        }
        assert_eq!(p.model.snapshot().fault, None);
        p.model.accept(p.time + 1000).unwrap();
        for _ in 0..120 {
            p.tick(&b);
        }
        assert_eq!(p.model.snapshot().state, State::Idle);
        for n in 0..240 {
            p.tick(if n % 2 == 0 { &a } else { &b });
        }
        assert_eq!(p.model.snapshot().state, State::Moving);
        for _ in 0..100 {
            p.tick(&b);
        }
        assert_eq!(p.model.snapshot().state, State::Idle);
        assert_eq!(p.model.snapshot().fault, None);
    }
}

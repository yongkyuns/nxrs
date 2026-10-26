//! Original experimental temporal features over owned Espressif motion vectors.
//! No detector admission or occupancy claim follows from a feature alone.
//! This module contains no ESPectre code. Pearson distance is a standard
//! mathematical operation; the native SDK remains the numerical witness.

pub const COLUMNS: usize = 64;
const REFERENCES: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    Shape,
    Nonfinite,
    Degenerate,
    SourceReversed,
    InvalidTime,
    Expired,
    ClockRollback,
}

/// One minus absolute Pearson correlation. Constant or unrepresentable input
/// is unavailable, rather than manufacturing a valid motion or clear score.
/// Arithmetic intentionally uses f32 operations without fused multiply-add.
pub fn distance(a: &[f32], b: &[f32]) -> Result<f32, Fault> {
    if a.len() < 2 || a.len() > COLUMNS || a.len() != b.len() {
        return Err(Fault::Shape);
    }
    if a.iter().chain(b).any(|v| !v.is_finite()) {
        return Err(Fault::Nonfinite);
    }
    let mut sum_a = 0.0f32;
    let mut sum_b = 0.0f32;
    for (&a, &b) in a.iter().zip(b) {
        sum_a += a;
        sum_b += b;
    }
    let mean_a = sum_a / a.len() as f32;
    let mean_b = sum_b / b.len() as f32;
    let (mut dot, mut aa, mut bb) = (0.0f32, 0.0f32, 0.0f32);
    for (&a, &b) in a.iter().zip(b) {
        let a = a - mean_a;
        let b = b - mean_b;
        dot += rounded_product(a, b);
        aa += rounded_product(a, a);
        bb += rounded_product(b, b);
    }
    let denominator = (aa * bb).sqrt();
    if !denominator.is_finite() || !dot.is_finite() {
        return Err(Fault::Nonfinite);
    }
    if denominator <= 0.0 {
        return Err(Fault::Degenerate);
    }
    let correlation = dot / denominator;
    if !correlation.is_finite() {
        return Err(Fault::Nonfinite);
    }
    Ok(1.0 - correlation.clamp(-1.0, 1.0).abs())
}

/// Materialize the f32 product before its subsequent addition. The pinned
/// Xtensa backend otherwise contracts ordinary `sum += a*b` into `madd.s`,
/// despite the source's separate IEEE operations. The SDK comparison kernel
/// rounds its products separately. The bit barrier is bounded, allocation-free
/// Rust; the target numerical and disassembly witnesses enforce this boundary.
#[inline(always)]
pub(crate) fn rounded_product(a: f32, b: f32) -> f32 {
    f32::from_bits(std::hint::black_box((a * b).to_bits()))
}

#[derive(Clone, Copy)]
pub struct Input<'a> {
    pub sequence: u32,
    pub window: u32,
    pub oldest_us: u64,
    pub newest_us: u64,
    pub values: &'a [f32],
}

#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub sequence: u32,
    pub window: u32,
    pub oldest_us: u64,
    pub newest_us: u64,
    pub value: f32,
}

#[derive(Clone, Copy)]
struct Owned {
    values: [f32; COLUMNS],
    oldest_us: u64,
}
const EMPTY: Owned = Owned {
    values: [0.0; COLUMNS],
    oldest_us: 0,
};

/// A maximum distance to three recent vectors, with bounded owned storage.
/// This is a declared feature experiment; it does not replace the historical
/// absolute-template model. The caller owns admission, phase boundaries and
/// any later temporal confirmation. There are no callbacks, locks or workers.
pub struct Temporal {
    previous: [Owned; REFERENCES],
    count: usize,
    next: usize,
    columns: usize,
    last: Option<(u32, u64, u32)>,
    last_clock: u64,
    freshness_us: u64,
    fault: Option<Fault>,
}

impl Temporal {
    pub fn new(freshness_us: u64) -> Option<Self> {
        (freshness_us > 0).then_some(Self {
            previous: [EMPTY; REFERENCES],
            count: 0,
            next: 0,
            columns: 0,
            last: None,
            last_clock: 0,
            freshness_us,
            fault: None,
        })
    }
    fn fail<T>(&mut self, reason: Fault) -> Result<T, Fault> {
        Err(*self.fault.get_or_insert(reason))
    }
    pub fn check_fresh(&mut self, now_us: u64) -> Result<(), Fault> {
        if let Some(fault) = self.fault {
            return Err(fault);
        }
        if now_us < self.last_clock {
            return self.fail(Fault::ClockRollback);
        }
        self.last_clock = now_us;
        if let Some((_, latest, _)) = self.last {
            if now_us - latest > self.freshness_us {
                return self.fail(Fault::Expired);
            }
        }
        Ok(())
    }
    pub fn observe(&mut self, input: Input<'_>, now_us: u64) -> Result<Option<Sample>, Fault> {
        self.check_fresh(now_us)?;
        if input.oldest_us > input.newest_us || input.newest_us > now_us {
            return self.fail(Fault::InvalidTime);
        }
        if now_us - input.oldest_us > self.freshness_us {
            return self.fail(Fault::Expired);
        }
        if input.values.len() < 2
            || input.values.len() > COLUMNS
            || (self.columns != 0 && self.columns != input.values.len())
            || input.window == 0
        {
            return self.fail(Fault::Shape);
        }
        if let Some((previous, time, window)) = self.last {
            let delta = input.sequence.wrapping_sub(previous);
            let window_delta = input.window.wrapping_sub(window);
            if delta == 0
                || delta >= 1 << 31
                || window_delta == 0
                || window_delta >= 1 << 31
                || input.newest_us <= time
            {
                return self.fail(Fault::SourceReversed);
            }
        }
        // Validate numerical support even during warmup. A constant profile
        // must not turn into a stored reference or an implicit zero score.
        if let Err(fault) = distance(input.values, input.values) {
            return self.fail(fault);
        }
        let mut oldest = input.oldest_us;
        let mut value = 0.0f32;
        for previous in &self.previous[..self.count] {
            oldest = oldest.min(previous.oldest_us);
            if now_us - oldest > self.freshness_us {
                return self.fail(Fault::Expired);
            }
            let compared = match distance(input.values, &previous.values[..input.values.len()]) {
                Ok(value) => value,
                Err(fault) => return self.fail(fault),
            };
            value = value.max(compared);
        }
        let sample = (self.count == REFERENCES).then_some(Sample {
            sequence: input.sequence,
            window: input.window,
            oldest_us: oldest,
            newest_us: input.newest_us,
            value,
        });
        self.previous[self.next].values[..input.values.len()].copy_from_slice(input.values);
        self.previous[self.next].oldest_us = input.oldest_us;
        self.columns = input.values.len();
        self.next = (self.next + 1) % REFERENCES;
        self.count = (self.count + 1).min(REFERENCES);
        self.last = Some((input.sequence, input.newest_us, input.window));
        Ok(sample)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn product_rounding_is_explicit_before_accumulation() {
        let a = f32::from_bits(0x3f800001);
        let c = -f32::from_bits(0x3f800002);
        let separate = c + rounded_product(a, a);
        assert_eq!(separate.to_bits(), 0);
        assert_ne!(a.mul_add(a, c).to_bits(), separate.to_bits());
    }
    fn input(sequence: u32, values: &[f32]) -> Input<'_> {
        Input {
            sequence,
            window: sequence,
            oldest_us: u64::from(sequence) * 1000 - 500,
            newest_us: u64::from(sequence) * 1000,
            values,
        }
    }
    #[test]
    fn affine_changes_preserve_shape_and_orthogonal_changes_do_not() {
        let a = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(distance(&a, &[12.0, 14.0, 16.0, 18.0]), Ok(0.0));
        assert_eq!(distance(&a, &[8.0, 6.0, 4.0, 2.0]), Ok(0.0));
        assert!(distance(&a, &[1.0, 4.0, 2.0, 3.0]).unwrap() > 0.5);
    }
    #[test]
    fn missing_numerical_support_is_unavailable() {
        assert_eq!(distance(&[0.0; 4], &[1.0; 4]), Err(Fault::Degenerate));
        assert_eq!(distance(&[f32::NAN; 4], &[1.0; 4]), Err(Fault::Nonfinite));
        assert_eq!(distance(&[1.0], &[1.0]), Err(Fault::Shape));
        assert_eq!(distance(&[f32::MAX; 4], &[1.0; 4]), Err(Fault::Nonfinite));
    }
    #[test]
    fn warmup_is_explicit_and_references_are_owned() {
        let mut p = Temporal::new(1_000_000).unwrap();
        let mut values = [1.0, 2.0, 3.0, 4.0];
        for n in 1..=3 {
            assert!(p
                .observe(input(n, &values), u64::from(n) * 1000)
                .unwrap()
                .is_none());
        }
        values = [1.0, 4.0, 2.0, 3.0];
        let s = p.observe(input(4, &values), 4000).unwrap().unwrap();
        assert_eq!(s.oldest_us, 500);
        assert_eq!(s.newest_us, 4000);
        assert_eq!(s.sequence, 4);
        assert_eq!(s.window, 4);
        assert!(s.value > 0.5);
    }
    #[test]
    fn a_profile_step_is_transient_and_sustained_changes_keep_evidence() {
        let a = [1.0, 2.0, 3.0, 4.0];
        let b = [1.0, 4.0, 2.0, 3.0];
        let mut p = Temporal::new(1_000_000).unwrap();
        for n in 1..=3 {
            p.observe(input(n, &a), u64::from(n) * 1000).unwrap();
        }
        for n in 4..=6 {
            assert!(
                p.observe(input(n, &b), u64::from(n) * 1000)
                    .unwrap()
                    .unwrap()
                    .value
                    > 0.5
            );
        }
        assert_eq!(p.observe(input(7, &b), 7000).unwrap().unwrap().value, 0.0);
        for n in 8..=20 {
            let v = if n % 2 == 0 { &a } else { &b };
            assert!(
                p.observe(input(n, v), u64::from(n) * 1000)
                    .unwrap()
                    .unwrap()
                    .value
                    > 0.5
            );
        }
    }
    #[test]
    fn faults_are_sticky_and_first_reason_is_preserved() {
        let mut p = Temporal::new(100_000).unwrap();
        assert_eq!(
            p.observe(input(1, &[0.0; 4]), 1000).unwrap_err(),
            Fault::Degenerate
        );
        assert_eq!(
            p.observe(input(2, &[1.0, 2.0, 3.0, 4.0]), 2000)
                .unwrap_err(),
            Fault::Degenerate
        );
        assert_eq!(p.check_fresh(0), Err(Fault::Degenerate));
    }
    #[test]
    fn silent_expiry_cannot_be_cleared_by_a_later_source() {
        let mut p = Temporal::new(10_000).unwrap();
        p.observe(input(1, &[1.0, 2.0, 3.0, 4.0]), 1000).unwrap();
        assert_eq!(p.check_fresh(11_001), Err(Fault::Expired));
        assert_eq!(
            p.observe(input(12, &[1.0, 2.0, 3.0, 4.0]), 12000)
                .unwrap_err(),
            Fault::Expired
        );
    }
    #[test]
    fn time_identity_and_shape_failures_are_explicit() {
        let a = [1.0, 2.0, 3.0, 4.0];
        for (change, expected) in [
            (0, Fault::SourceReversed),
            (1, Fault::InvalidTime),
            (2, Fault::Shape),
            (3, Fault::ClockRollback),
        ] {
            let mut p = Temporal::new(10_000).unwrap();
            p.observe(input(1, &a), 1000).unwrap();
            let mut i = input(2, &a);
            let mut now = 2000;
            match change {
                0 => i.sequence = 1,
                1 => i.newest_us = 2001,
                2 => i.values = &a[..3],
                _ => now = 999,
            }
            assert_eq!(p.observe(i, now).unwrap_err(), expected);
        }
    }
}

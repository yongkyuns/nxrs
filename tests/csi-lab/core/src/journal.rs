//! Versioned, bounded records. Source time is payload data; `time_us` is the
//! detector clock sampled under model ownership. Replay never uses wall time.
use crate::calibration::{Calibration, Config, Feature, Rejection, Snapshot};
use std::io::{self, Write};

pub const VERSION: u32 = 1;
pub const WORDS: usize = 256;
pub const SESSION: u32 = 1;
pub const MODEL: u32 = 2;
pub const RAW: u32 = 3;
pub const GAIN: u32 = 4;
pub const MATRIX: u32 = 5;
pub const WANDER: u32 = 6;
pub const LABEL: u32 = 7;
pub const END: u32 = 8;
pub const CONTROL: u32 = 9;
const MAGIC: u32 = 0x4c495343;

#[derive(Clone, Copy)]
pub struct Record {
    pub sequence: u32,
    pub time_us: u64,
    pub kind: u32,
    pub len: usize,
    pub words: [u32; WORDS],
}
impl Record {
    pub const EMPTY: Self = Self {
        sequence: 0,
        time_us: 0,
        kind: 0,
        len: 0,
        words: [0; WORDS],
    };
    pub fn new(kind: u32, time_us: u64, words: &[u32]) -> Result<Self, &'static str> {
        if !(SESSION..=CONTROL).contains(&kind) || words.len() > WORDS {
            return Err("record bounds");
        }
        let mut r = Self {
            kind,
            time_us,
            len: words.len(),
            ..Self::EMPTY
        };
        r.words[..r.len].copy_from_slice(words);
        Ok(r)
    }
    pub fn data(&self) -> &[u32] {
        &self.words[..self.len]
    }
    pub fn checksum(&self) -> u32 {
        crc32(self.header().into_iter().chain(self.data().iter().copied()))
    }
    fn header(&self) -> [u32; 7] {
        [
            MAGIC,
            VERSION,
            self.sequence,
            self.time_us as u32,
            (self.time_us >> 32) as u32,
            self.kind,
            self.len as u32,
        ]
    }
    // Text is only a USB transport envelope. Numeric values preserve exact bits.
    pub fn write(&self, out: &mut impl Write) -> io::Result<()> {
        self.write_crc(out, self.checksum())
    }
    pub(crate) fn write_crc(&self, out: &mut impl Write, crc: u32) -> io::Result<()> {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut line = Vec::with_capacity(8 * (self.len + 8) + 9);
        line.extend_from_slice(b"CSI_LAB ");
        for word in self
            .header()
            .into_iter()
            .chain(self.data().iter().copied())
            .chain([crc])
        {
            for shift in (0..8).rev() {
                line.push(HEX[((word >> (4 * shift)) & 15) as usize]);
            }
        }
        line.push(b'\n');
        out.write_all(&line)
    }
    pub fn parse(line: &str) -> Result<Self, &'static str> {
        let s = line.strip_prefix("CSI_LAB ").ok_or("record prefix")?;
        if s.len() < 64 || s.len() > (WORDS + 8) * 8 || s.len() % 8 != 0 {
            return Err("record length");
        }
        let mut w = [0u32; WORDS + 8];
        for (i, part) in s.as_bytes().chunks_exact(8).enumerate() {
            w[i] = u32::from_str_radix(
                std::str::from_utf8(part).map_err(|_| "record encoding")?,
                16,
            )
            .map_err(|_| "record hex")?;
        }
        let n = w[6] as usize;
        if w[0] != MAGIC || w[1] != VERSION {
            return Err("record version");
        }
        if n > WORDS || s.len() != (n + 8) * 8 {
            return Err("record payload length");
        }
        let mut r = Self::new(
            w[5],
            u64::from(w[3]) | (u64::from(w[4]) << 32),
            &w[7..7 + n],
        )?;
        r.sequence = w[2];
        if r.checksum() != w[7 + n] {
            return Err("record CRC");
        }
        Ok(r)
    }
}
const fn crc_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut v = i as u32;
        let mut bit = 0;
        while bit < 8 {
            v = (v >> 1) ^ (0xedb88320 & 0u32.wrapping_sub(v & 1));
            bit += 1;
        }
        table[i] = v;
        i += 1;
    }
    table
}
const CRC_TABLE: [u32; 256] = crc_table();
pub fn crc32(words: impl Iterator<Item = u32>) -> u32 {
    let mut crc = !0u32;
    for byte in words.flat_map(u32::to_le_bytes) {
        crc = (crc >> 8) ^ CRC_TABLE[((crc ^ u32::from(byte)) & 255) as usize];
    }
    !crc
}
pub fn session(config: Config) -> Record {
    Record::new(
        SESSION,
        0,
        &[
            config.minimum_features,
            config.minimum_duration_us as u32,
            (config.minimum_duration_us >> 32) as u32,
            config.freshness_us as u32,
            (config.freshness_us >> 32) as u32,
            config.sensitivity.to_bits(),
            config.minimum_margin.to_bits(),
        ],
    )
    .unwrap()
}
pub fn config(record: &Record) -> Result<Config, &'static str> {
    let w = record.data();
    if record.kind != SESSION || w.len() != 7 || record.time_us != 0 {
        return Err("session schema");
    }
    let c = Config {
        minimum_features: w[0],
        minimum_duration_us: u64::from(w[1]) | (u64::from(w[2]) << 32),
        freshness_us: u64::from(w[3]) | (u64::from(w[4]) << 32),
        sensitivity: f32::from_bits(w[5]),
        minimum_margin: f32::from_bits(w[6]),
    };
    Calibration::new(c).ok_or("invalid config")?;
    Ok(c)
}

#[derive(Clone, Copy)]
pub enum Operation {
    Start(u32),
    Validate,
    Accept,
    Observe(Feature),
    Poll,
}
impl Operation {
    pub fn apply(self, value: &mut Calibration, now: u64) -> bool {
        match self {
            Self::Start(templates) => value.start(now, templates),
            Self::Validate => value.validate(now),
            Self::Accept => value.accept(now),
            Self::Observe(feature) => {
                value.observe(feature, now);
                true
            }
            Self::Poll => {
                value.expire(now);
                true
            }
        }
    }
}
pub fn checkpoint(s: Snapshot) -> [u32; 9] {
    [
        s.phase as u32,
        s.state as u32,
        s.rejection as u32,
        s.features,
        s.average.to_bits(),
        s.lower.to_bits(),
        s.upper.to_bits(),
        s.enter.to_bits(),
        s.exit.to_bits(),
    ]
}
/// Small transaction evidence; callbacks never carry the maximum wire record.
#[derive(Clone, Copy)]
pub struct ModelEvent {
    pub time_us: u64,
    pub words: [u32; 25],
}
impl ModelEvent {
    pub fn to_record(&self) -> Record {
        Record::new(MODEL, self.time_us, &self.words).unwrap()
    }
}
pub fn model(
    op: Operation,
    now: u64,
    before: Rejection,
    after: Rejection,
    accepted: bool,
    s: Snapshot,
) -> Record {
    model_event(op, now, before, after, accepted, s).to_record()
}
pub fn model_event(
    op: Operation,
    now: u64,
    before: Rejection,
    after: Rejection,
    accepted: bool,
    s: Snapshot,
) -> ModelEvent {
    let mut w = [0u32; 25];
    w[1] = before as u32;
    w[2] = after as u32;
    w[3] = u32::from(accepted);
    w[4..13].copy_from_slice(&checkpoint(s));
    match op {
        Operation::Start(t) => {
            w[0] = 1;
            w[13] = t;
        }
        Operation::Validate => w[0] = 2,
        Operation::Accept => w[0] = 3,
        Operation::Observe(f) => {
            w[0] = 4;
            w[13..23].copy_from_slice(&[
                f.sequence,
                f.oldest_us as u32,
                (f.oldest_us >> 32) as u32,
                f.newest_us as u32,
                (f.newest_us >> 32) as u32,
                f.templates,
                u32::from(f.training_complete),
                u32::from(f.gain_available),
                f.wander.to_bits(),
                0,
            ]);
        }
        Operation::Poll => w[0] = 5,
    }
    ModelEvent {
        time_us: now,
        words: w,
    }
}
fn rejection(w: u32) -> Result<Rejection, &'static str> {
    // Only runtime-origin failures may be injected by the recording. Algorithm
    // rejections must be reproduced by the algorithm, not supplied as answers.
    match w {
        0 => Ok(Rejection::None),
        1 => Ok(Rejection::TraceFault),
        2 => Ok(Rejection::NativeError),
        3 => Ok(Rejection::SourceContention),
        4 => Ok(Rejection::ControlContention),
        5 => Ok(Rejection::Poisoned),
        _ => Err("external fault code"),
    }
}
pub fn replay_model(
    value: &mut Calibration,
    record: &Record,
    verify: bool,
) -> Result<Snapshot, &'static str> {
    let w = record.data();
    if record.kind != MODEL || w.len() != 25 || w[3] > 1 || (w[22] != 0 || w[24] != 0) {
        return Err("model schema");
    }
    let before = rejection(w[1])?;
    let after = rejection(w[2])?;
    let op = match w[0] {
        1 => Operation::Start(w[13]),
        2 => Operation::Validate,
        3 => Operation::Accept,
        5 => Operation::Poll,
        4 if w[19] <= 1 && w[20] <= 1 => Operation::Observe(Feature {
            sequence: w[13],
            oldest_us: u64::from(w[14]) | (u64::from(w[15]) << 32),
            newest_us: u64::from(w[16]) | (u64::from(w[17]) << 32),
            templates: w[18],
            training_complete: w[19] != 0,
            gain_available: w[20] != 0,
            wander: f32::from_bits(w[21]),
        }),
        _ => return Err("operation schema"),
    };
    if before != Rejection::None {
        value.reject(before);
    }
    let accepted = op.apply(value, record.time_us);
    if after != Rejection::None {
        value.reject(after);
    }
    let s = value.checkpoint();
    if verify && (accepted != (w[3] != 0) || checkpoint(s) != w[4..13]) {
        return Err("live/replay checkpoint mismatch");
    }
    Ok(s)
}

/// Validates order, completeness and the rolling CRC chain. END is required;
/// transport losses invalidate the whole capture, even if its final state looks good.
#[derive(Default)]
pub struct Integrity {
    next: u32,
    chain: u32,
    ended: bool,
}
impl Integrity {
    pub fn accept(&mut self, r: &Record) -> Result<(), &'static str> {
        if self.ended || r.sequence != self.next {
            return Err("record sequence/epoch");
        }
        if self.next == 0 && r.kind != SESSION || self.next != 0 && r.kind == SESSION {
            return Err("session boundary");
        }
        if r.kind == END {
            if r.data() != [self.next, self.chain, 0] {
                return Err("incomplete capture or loss");
            }
            self.ended = true;
        } else {
            self.chain = chain(self.chain, r.checksum());
        }
        self.next = self.next.checked_add(1).ok_or("sequence overflow")?;
        Ok(())
    }
    pub fn finish(&self) -> Result<(), &'static str> {
        if self.ended {
            Ok(())
        } else {
            Err("missing END; truncated capture")
        }
    }
}
pub fn chain(previous: u32, checksum: u32) -> u32 {
    crc32([previous, checksum].into_iter())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        calibration::{Runtime, State},
        recorder::Recorder,
    };
    fn parse(bytes: &[u8]) -> Vec<Record> {
        std::str::from_utf8(bytes)
            .unwrap()
            .lines()
            .map(|s| Record::parse(s).unwrap())
            .collect()
    }
    #[test]
    fn live_transactions_replay_bit_exact_across_background_holdout_motion_return_and_silence() {
        let c = Config {
            minimum_features: 25,
            minimum_duration_us: 1000,
            ..Config::EMPTY_HOLDOUT
        };
        let runtime = Runtime::new();
        assert!(runtime.prepare(c));
        let recorder = Recorder::new();
        assert!(recorder.prepare(c, 256));
        let mut out = Vec::new();
        let mut n = 0u32;
        runtime
            .execute(Operation::Start(10), || 0, |r| recorder.push_model(r))
            .unwrap();
        for phase in 0..4 {
            if phase == 1 {
                assert!(
                    runtime
                        .execute(
                            Operation::Validate,
                            || u64::from(n) * 1000,
                            |r| recorder.push_model(r)
                        )
                        .unwrap()
                        .0
                );
            }
            if phase == 2 {
                assert!(
                    runtime
                        .execute(
                            Operation::Accept,
                            || u64::from(n) * 1000,
                            |r| recorder.push_model(r)
                        )
                        .unwrap()
                        .0
                );
            }
            for _ in 0..60 {
                n += 1;
                let now = u64::from(n) * 1000;
                runtime
                    .execute(
                        Operation::Observe(Feature {
                            sequence: n,
                            oldest_us: now,
                            newest_us: now,
                            templates: 10,
                            training_complete: true,
                            gain_available: true,
                            wander: if phase == 2 { 0.02 } else { 1.0 / 1024.0 },
                        }),
                        || now,
                        |r| recorder.push_model(r),
                    )
                    .unwrap();
            }
            recorder.drain(&mut out).unwrap();
        }
        runtime
            .execute(
                Operation::Poll,
                || u64::from(n) * 1000 + 1_000_001,
                |r| recorder.push_model(r),
            )
            .unwrap();
        recorder.finish(1_500_000, &mut out).unwrap();
        let mut integrity = Integrity::default();
        let mut replay = Calibration::new(c).unwrap();
        let mut states = Vec::new();
        for r in parse(&out) {
            integrity.accept(&r).unwrap();
            if r.kind == MODEL {
                states.push(replay_model(&mut replay, &r, true).unwrap().state);
            }
        }
        integrity.finish().unwrap();
        assert!(states.contains(&State::PresenceHint));
        assert!(states.contains(&State::NotDetected));
        assert_eq!(states.last(), Some(&State::Unknown));
    }
    #[test]
    fn crc_version_length_sequence_loss_and_missing_footer_are_rejected() {
        let r = session(Config::EMPTY_HOLDOUT);
        let mut bytes = Vec::new();
        r.write(&mut bytes).unwrap();
        let text = String::from_utf8(bytes).unwrap();
        let mut corrupted = text.clone().into_bytes();
        corrupted[70] = if corrupted[70] == b'0' { b'1' } else { b'0' };
        assert!(Record::parse(std::str::from_utf8(&corrupted).unwrap().trim_end()).is_err());
        assert!(Record::parse(&text.trim_end()[..text.trim_end().len() - 8]).is_err());
        let bad = text.replacen("00000001", "00000002", 1);
        assert!(Record::parse(bad.trim_end()).is_err());
        let mut i = Integrity::default();
        i.accept(&r).unwrap();
        assert!(i.accept(&r).is_err());
        assert!(i.finish().is_err());
        let mut end = Record::new(END, 0, &[1, chain(0, r.checksum()), 1]).unwrap();
        end.sequence = 1;
        assert!(i.accept(&end).is_err());
    }
    #[test]
    fn checkpoints_cannot_supply_algorithm_rejections_as_external_answers() {
        let mut c = Calibration::new(Config::EMPTY_HOLDOUT).unwrap();
        let mut r = model(
            Operation::Poll,
            0,
            Rejection::None,
            Rejection::None,
            true,
            c.checkpoint(),
        );
        r.words[1] = Rejection::HoldoutExceeded as u32;
        assert!(replay_model(&mut c, &r, true).is_err());
        r.words[1] = 0;
        r.words[8] ^= 1;
        assert!(replay_model(&mut c, &r, true).is_err());
    }
    #[test]
    fn external_native_fault_and_real_clock_rollback_reproduce_the_actual_rejection() {
        for fault in [false, true] {
            let runtime = Runtime::new();
            assert!(runtime.prepare(Config::EMPTY_HOLDOUT));
            let mut records = Vec::new();
            runtime
                .execute(Operation::Start(10), || 100, |r| records.push(r))
                .unwrap();
            if fault {
                runtime.fail(Rejection::NativeError);
            }
            runtime
                .execute(
                    Operation::Poll,
                    || if fault { 200 } else { 99 },
                    |r| records.push(r),
                )
                .unwrap();
            let mut c = Calibration::new(Config::EMPTY_HOLDOUT).unwrap();
            for r in records {
                replay_model(&mut c, &r.to_record(), true).unwrap();
            }
            assert_eq!(
                c.checkpoint().rejection,
                if fault {
                    Rejection::NativeError
                } else {
                    Rejection::ClockRollback
                }
            );
        }
    }
}

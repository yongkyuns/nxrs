//! Exact RAW -> gain-input -> gain-output joins; never infer window identity.
use crate::{header, line, replay, Result};
use csi_lab_core::journal::{self, Integrity, Record};
use serde_json::json;
use std::{fs::File, io::BufReader};

#[derive(Default)]
struct Counts {
    pairs: u64,
    zero_input_buffers: u64,
    zero_output_buffers: u64,
    successful_nonzero_to_zero: u64,
}
impl Counts {
    fn value(&self) -> serde_json::Value {
        json!({"pairs":self.pairs,"zero_input_buffers":self.zero_input_buffers,
               "zero_output_buffers":self.zero_output_buffers,
               "successful_nonzero_to_zero":self.successful_nonzero_to_zero})
    }
}
#[derive(Default)]
struct Audit {
    source: Option<[u8; 6]>,
    raw: Option<Vec<u32>>,
    before: Option<Vec<u32>>,
    counts: [Counts; 2],
}
fn bytes(words: &[u32]) -> Vec<u8> {
    words.iter().flat_map(|w| w.to_le_bytes()).collect()
}
impl Audit {
    fn accept(&mut self, r: &Record) -> std::result::Result<(), &'static str> {
        let w = r.data();
        match r.kind {
            journal::CONTROL if w.first() == Some(&3) => {
                if self.source.is_some() || w.len() != 4 {
                    return Err("gain audit source declaration");
                }
                self.source = Some(bytes(&w[2..4])[..6].try_into().unwrap());
            }
            journal::RAW => {
                if self.source.is_none()
                    || self.raw.is_some()
                    || self.before.is_some()
                    || w.len() < 11
                    || w[6] > 1
                    || w[7] == 0
                    || w[7] > 612
                    || w.len() != 11 + (w[7] as usize).div_ceil(4)
                {
                    return Err("gain audit raw order/schema");
                }
                self.raw = Some(w.to_vec());
            }
            journal::GAIN => {
                let raw = self.raw.as_ref().ok_or("gain audit missing raw")?;
                if w.len() < 10
                    || w[2] > 1
                    || w[1] != raw[0]
                    || w[6] != raw[4]
                    || w[7] != raw[5]
                    || w[8] != raw[6]
                    || w[9] != raw[7]
                    || w.len() != 10 + (w[9] as usize).div_ceil(4)
                {
                    return Err("gain audit call identity/schema");
                }
                let n = raw[7] as usize;
                if w[2] == 0 {
                    if self.before.is_some() || bytes(&w[10..])[..n] != bytes(&raw[11..])[..n] {
                        return Err("gain audit filtered input differs");
                    }
                    self.before = Some(w.to_vec());
                } else {
                    let before = self.before.as_ref().ok_or("gain audit missing input")?;
                    if before[0] != w[0] || before[5..10] != w[5..10] {
                        return Err("gain audit input/output identity");
                    }
                    let macs = bytes(&raw[8..11]);
                    let source = self.source.unwrap();
                    let selected = macs[..6] == source && macs[6..12] == source;
                    let c = &mut self.counts[usize::from(!selected)];
                    let input_zero = bytes(&before[10..])[..n].iter().all(|b| *b == 0);
                    let output_zero = bytes(&w[10..])[..n].iter().all(|b| *b == 0);
                    c.pairs += 1;
                    c.zero_input_buffers += u64::from(input_zero);
                    c.zero_output_buffers += u64::from(output_zero);
                    c.successful_nonzero_to_zero +=
                        u64::from(w[3] == 0 && !input_zero && output_zero);
                    self.raw = None;
                    self.before = None;
                }
            }
            _ => {}
        }
        Ok(())
    }
    fn finish(&self) -> std::result::Result<(), &'static str> {
        if self.source.is_none()
            || self.raw.is_some()
            || self.before.is_some()
            || self.counts.iter().map(|c| c.pairs).sum::<u64>() == 0
        {
            return Err("gain audit incomplete or missing pairs");
        }
        Ok(())
    }
}
pub fn run(path: &str) -> Result<()> {
    replay(path, None, false)?;
    let mut reader = BufReader::new(File::open(path)?);
    let _ = header(&mut reader)?;
    let mut audit = Audit::default();
    let mut integrity = Integrity::default();
    while let Some(line) = line(&mut reader)? {
        let r = Record::parse(&line)?;
        integrity.accept(&r)?;
        audit.accept(&r)?;
    }
    integrity.finish()?;
    audit.finish()?;
    println!(
        "{}",
        json!({"original_capture_verified":true,
        "selected":audit.counts[0].value(),"other":audit.counts[1].value(),
        "scope":"Exact recorded filtered-IQ/native-gain byte-buffer transitions. No physical driver raw IQ, LTF/window identity, detection accuracy or CPU-cost inference."})
    );
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn record(kind: u32, words: &[u32]) -> Record {
        Record::new(kind, 1, words).unwrap()
    }
    fn source() -> Record {
        record(journal::CONTROL, &[3, 6, 0x05040302, 0x0706])
    }
    fn raw(other: bool) -> Record {
        let mut macs = [2, 3, 4, 5, 6, 7, 2, 3, 4, 5, 6, 7];
        if other {
            macs[6] = 8;
        }
        let mut w = vec![7, 0, 100, 0, 33, 0, 0, 4];
        w.extend(
            macs.chunks_exact(4)
                .map(|c| u32::from_le_bytes(c.try_into().unwrap())),
        );
        w.push(0x04030201);
        record(journal::RAW, &w)
    }
    fn gain(after: bool) -> Record {
        record(
            journal::GAIN,
            &[
                1,
                7,
                u32::from(after),
                0,
                0.03f32.to_bits(),
                1,
                33,
                0,
                0,
                4,
                if after { 0 } else { 0x04030201 },
            ],
        )
    }
    #[test]
    fn exact_pairs_preserve_source_and_native_zero_transition() {
        let mut a = Audit::default();
        a.accept(&source()).unwrap();
        for other in [false, true] {
            for r in [raw(other), gain(false), gain(true)] {
                a.accept(&r).unwrap();
            }
        }
        a.finish().unwrap();
        for c in a.counts {
            assert_eq!(
                (c.pairs, c.successful_nonzero_to_zero, c.zero_input_buffers),
                (1, 1, 0)
            );
        }
    }
    #[test]
    fn mismatched_raw_or_gain_identity_and_incomplete_pairs_fail() {
        for (stage, word) in [(0, 10), (1, 0), (1, 6)] {
            let mut a = Audit::default();
            a.accept(&source()).unwrap();
            a.accept(&raw(false)).unwrap();
            if stage == 1 {
                a.accept(&gain(false)).unwrap();
            }
            let mut bad = gain(stage == 1);
            bad.words[word] ^= 1;
            assert!(a.accept(&bad).is_err());
        }
        let mut a = Audit::default();
        a.accept(&source()).unwrap();
        assert!(a.accept(&gain(false)).is_err());
        a.accept(&raw(false)).unwrap();
        assert!(a.finish().is_err());
        assert!(a.accept(&raw(false)).is_err());
    }
}

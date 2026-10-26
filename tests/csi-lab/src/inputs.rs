//! Host-side integrity for captured native call inputs. This does not execute
//! the vendor engine: it reconstructs complete bit-exact calls for target replay.
use csi_lab_core::journal::{self, Record};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Default)]
pub struct Inputs {
    pending: BTreeMap<(u32, u32), Call>,
    completed: BTreeSet<(u32, u32)>,
    gains: BTreeMap<u32, Vec<u32>>,
}
struct Call {
    cols: u32,
    rows0: u32,
    rows1: u32,
    parts: [Vec<u32>; 2],
}
fn bytes(w: &[u32], header: usize, size: usize) -> Result<(), &'static str> {
    if size == 0 || size > 612 || w.len() != header + size.div_ceil(4) {
        return Err("byte record size");
    }
    if size & 3 != 0 && w[w.len() - 1] >> (8 * (size % 4)) != 0 {
        return Err("byte padding");
    }
    Ok(())
}
impl Inputs {
    pub fn accept(&mut self, r: &Record) -> Result<Option<Value>, &'static str> {
        let w = r.data();
        match r.kind {
            journal::RAW => {
                if w.len() < 11 {
                    return Err("raw header");
                }
                bytes(w, 11, w[7] as usize)?;
            }
            journal::GAIN => {
                if w.len() < 10 || w[2] > 1 || w[8] > 1 {
                    return Err("gain header");
                }
                bytes(w, 10, w[9] as usize)?;
                if w[2] == 0 {
                    if self.gains.len() >= 64 || self.gains.insert(w[0], w.to_vec()).is_some() {
                        return Err("gain pair overlap");
                    }
                } else {
                    let before = self.gains.remove(&w[0]).ok_or("missing gain input")?;
                    if before[1] != w[1] || before[5..10] != w[5..10] {
                        return Err("gain input/output identity");
                    }
                }
            }
            journal::MATRIX | journal::WANDER => {
                if w.len() >= 8 && w[1] == 3 {
                    let key = (r.kind, w[0]);
                    let n = w[2] as usize;
                    if r.kind != journal::WANDER
                        || n == 0
                        || n > journal::PACKED_WANDER_COLS
                        || w[3..5] != [0, 0]
                        || w[5] as usize != 2 * n + 1
                        || w[6..8] != [0, 0]
                        || w.len() != 9 + 2 * n
                        || self.completed.contains(&key)
                        || self.pending.contains_key(&key)
                    {
                        return Err("packed wander shape or identity");
                    }
                    self.completed.insert(key);
                    return Ok(Some(
                        json!({"kind":"wander","id":w[0],"cols":w[2],"rows0":0,"rows1":0,"input0_bits":&w[8..8+n],"input1_bits":&w[8+n..8+2*n],"status":0,"output_bits":&w[8+2*n..]}),
                    ));
                }
                if w.len() < 8 || w[1] > 2 || w[2] == 0 || w[2] > 256 || w[5] > 4096 || w[6] > w[5]
                {
                    return Err("numerical chunk header");
                }
                if r.kind == journal::WANDER && w[7] != 0 {
                    return Err("wander has no native status");
                }
                let key = (r.kind, w[0]);
                if self.completed.contains(&key) {
                    return Err("duplicate numerical call");
                }
                if !self.pending.contains_key(&key) {
                    if self.pending.len() >= 64 || w[1] != 0 || w[6] != 0 {
                        return Err("numerical call admission");
                    }
                    if r.kind == journal::MATRIX
                        && (w[2] > 64
                            || (w[3] == 0 && w[4] == 0)
                            || w[3]
                                .checked_add(w[4])
                                .and_then(|v| v.checked_mul(w[2]))
                                .is_none_or(|v| v > 4096))
                    {
                        return Err("matrix shape");
                    }
                    if r.kind == journal::WANDER && (w[3] != 0 || w[4] != 0) {
                        return Err("wander shape");
                    }
                    self.pending.insert(
                        key,
                        Call {
                            cols: w[2],
                            rows0: w[3],
                            rows1: w[4],
                            parts: [Vec::new(), Vec::new()],
                        },
                    );
                }
                let call = self.pending.get_mut(&key).unwrap();
                if (call.cols, call.rows0, call.rows1) != (w[2], w[3], w[4]) {
                    return Err("changed numerical shape");
                }
                let expected = if r.kind == journal::WANDER {
                    [call.cols as usize, call.cols as usize, 1]
                } else {
                    [
                        (call.cols * call.rows0) as usize,
                        (call.cols * call.rows1) as usize,
                        if w[7] == 0 { call.cols as usize } else { 0 },
                    ]
                };
                let part = w[1] as usize;
                let count = w.len() - 8;
                if w[5] as usize != expected[part]
                    || count != (expected[part] - w[6] as usize).min(240)
                {
                    return Err("numerical chunk length");
                }
                if part < 2 {
                    if w[7] != 0 || w[6] as usize != call.parts[part].len() {
                        return Err("numerical input order/status");
                    }
                    call.parts[part].extend_from_slice(&w[8..]);
                } else {
                    if w[6] != 0
                        || w[5] > 240
                        || call.parts[0].len() != expected[0]
                        || call.parts[1].len() != expected[1]
                    {
                        return Err("incomplete numerical call");
                    }
                    let call = self.pending.remove(&key).unwrap();
                    self.completed.insert(key);
                    return Ok(Some(
                        json!({"kind":if r.kind==journal::MATRIX{"motion"}else{"wander"},"id":w[0],"cols":w[2],"rows0":w[3],"rows1":w[4],"input0_bits":call.parts[0],"input1_bits":call.parts[1],"status":w[7] as i32,"output_bits":&w[8..]}),
                    ));
                }
            }
            _ => {}
        }
        Ok(None)
    }
    pub fn finish(&self) -> Result<(), &'static str> {
        if self.pending.is_empty() && self.gains.is_empty() {
            Ok(())
        } else {
            Err("native call incomplete at capture boundary")
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn r(kind: u32, words: &[u32]) -> Record {
        Record::new(kind, 0, words).unwrap()
    }
    #[test]
    fn native_calls_reassemble_exact_bits_and_shape_and_reject_reuse() {
        let mut i = Inputs::default();
        i.accept(&r(
            journal::MATRIX,
            &[9, 0, 2, 1, 1, 2, 0, 0, 0x3f800000, 0x40000000],
        ))
        .unwrap();
        i.accept(&r(
            journal::MATRIX,
            &[9, 1, 2, 1, 1, 2, 0, 0, 0x40400000, 0x40800000],
        ))
        .unwrap();
        assert!(i.finish().is_err());
        let out = r(
            journal::MATRIX,
            &[9, 2, 2, 1, 1, 2, 0, 0, 0x40a00000, 0x40c00000],
        );
        let call = i.accept(&out).unwrap().unwrap();
        assert_eq!(call["input1_bits"][0], 0x40400000u32);
        i.finish().unwrap();
        assert!(i.accept(&out).is_err());
    }
    #[test]
    fn wrong_offsets_failed_output_and_malformed_radio_are_rejected() {
        let mut i = Inputs::default();
        assert!(i
            .accept(&r(journal::MATRIX, &[1, 0, 2, 1, 0, 2, 1, 0, 0]))
            .is_err());
        assert!(i.accept(&r(journal::RAW, &[0; 11])).is_err());
        let mut i = Inputs::default();
        i.accept(&r(journal::MATRIX, &[1, 0, 2, 1, 0, 2, 0, 0, 0, 0]))
            .unwrap();
        i.accept(&r(journal::MATRIX, &[1, 1, 2, 1, 0, 0, 0, 0]))
            .unwrap();
        let c = i
            .accept(&r(journal::MATRIX, &[1, 2, 2, 1, 0, 0, 0, (-1i32) as u32]))
            .unwrap()
            .unwrap();
        assert_eq!(c["status"], -1);
        assert_eq!(c["output_bits"], json!([]));
    }
    #[test]
    fn gain_requires_matching_before_after_and_complete_boundary() {
        let mut i = Inputs::default();
        let mut w = [1, 2, 0, 0, 0, 1, 20, 4, 0, 1, 0x7f];
        i.accept(&r(journal::GAIN, &w)).unwrap();
        assert!(i.finish().is_err());
        w[2] = 1;
        w[10] = 0x3f;
        i.accept(&r(journal::GAIN, &w)).unwrap();
        i.finish().unwrap();
        assert!(i.accept(&r(journal::GAIN, &w)).is_err());
    }
}

#[cfg(test)]
mod packed_tests {
    use super::*;
    #[test]
    fn packed_and_chunked_calls_export_identical_bits_and_reject_cross_format_reuse() {
        let mut packed = Inputs::default();
        let record = Record::new(
            journal::WANDER,
            0,
            &[1, 3, 2, 0, 0, 5, 0, 0, 10, 20, 30, 40, 50],
        )
        .unwrap();
        let a = packed.accept(&record).unwrap().unwrap();
        packed.finish().unwrap();
        let mut chunked = Inputs::default();
        let mut b = None;
        for words in [
            &[1, 0, 2, 0, 0, 2, 0, 0, 10, 20][..],
            &[1, 1, 2, 0, 0, 2, 0, 0, 30, 40],
            &[1, 2, 2, 0, 0, 1, 0, 0, 50],
        ] {
            b = chunked
                .accept(&Record::new(journal::WANDER, 0, words).unwrap())
                .unwrap();
        }
        assert_eq!(a, b.unwrap());
        chunked.finish().unwrap();
        assert!(chunked.accept(&record).is_err());
        assert!(packed.accept(&record).is_err());
        let first = Record::new(journal::WANDER, 0, &[1, 0, 2, 0, 0, 2, 0, 0, 10, 20]).unwrap();
        assert!(packed.accept(&first).is_err());
        let mut pending = Inputs::default();
        pending.accept(&first).unwrap();
        assert!(pending.accept(&record).is_err());
    }
    #[test]
    fn packed_shape_status_offset_length_and_wrong_kind_are_rejected() {
        let good = [1, 3, 2, 0, 0, 5, 0, 0, 10, 20, 30, 40, 50];
        for (index, value) in [(2, 0), (2, 124), (3, 1), (4, 1), (5, 4), (6, 1), (7, 1)] {
            let mut words = good;
            words[index] = value;
            assert!(Inputs::default()
                .accept(&Record::new(journal::WANDER, 0, &words).unwrap())
                .is_err());
        }
        assert!(Inputs::default()
            .accept(&Record::new(journal::MATRIX, 0, &good).unwrap())
            .is_err());
        assert!(Inputs::default()
            .accept(&Record::new(journal::WANDER, 0, &good[..12]).unwrap())
            .is_err());
    }
}

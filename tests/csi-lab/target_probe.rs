//! Test-only bounded input protocol for exact host/Xtensa policy comparison.
//! No radio, SDK callback, hardware access, tuning or detector admission.
use csi_lab_core::{
    calibration::Feature,
    motion_pipeline::{Pipeline, Profile, COLUMNS},
    motion_trial::{Event, Label, Update},
    motion_wire::Checkpoint,
    temporal,
};
use std::io::{self, Write};

pub const MAGIC: &[u8; 8] = b"CSIMP001";
pub const BEGIN: u32 = 1;
pub const CLOCK: u32 = 2;
pub const LABEL: u32 = 3;
pub const TRAIN_STOP: u32 = 4;
pub const SOURCE: u32 = 5;
pub const DISTANCE: u32 = 6;
pub const END: u32 = 7;
const MAX_WORDS: usize = 2 + 2 * temporal::COLUMNS;
pub const MAX_BYTES: usize = 4 * 1024 * 1024;
const MAX_EVENTS: u32 = 100_000;
#[derive(Debug, PartialEq, Eq)]
pub struct Report {
    pub cases: u32,
    pub events: u32,
    pub checkpoints: u32,
    pub distances: u32,
}
#[derive(Debug)]
pub enum Error {
    Schema(&'static str),
    Arithmetic {
        case: u32,
        comparison: u32,
        expected: u32,
        actual: u32,
    },
}
impl From<&'static str> for Error {
    fn from(reason: &'static str) -> Self {
        Self::Schema(reason)
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {Self::Schema(reason)=>f.write_str(reason),Self::Arithmetic {case,comparison,expected,actual}=>write!(f,"probe Rust/native comparison bits differ case={case} comparison={comparison} expected={expected:08x} actual={actual:08x}")}
    }
}
impl std::error::Error for Error {}

fn time(w: &[u32]) -> u64 {
    u64::from(w[0]) | u64::from(w[1]) << 32
}
fn flag(w: u32) -> Result<bool, &'static str> {
    match w {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err("probe boolean"),
    }
}
fn body(input: &[u8], offset: &mut usize) -> Result<(u32, [u32; MAX_WORDS], usize), &'static str> {
    let head = input
        .get(*offset..offset.checked_add(8).ok_or("probe overflow")?)
        .ok_or("probe header truncated")?;
    *offset += 8;
    let tag = u32::from_le_bytes(head[..4].try_into().unwrap());
    let n = u32::from_le_bytes(head[4..].try_into().unwrap()) as usize;
    if n > MAX_WORDS {
        return Err("probe record bound");
    }
    let data = input
        .get(*offset..offset.checked_add(n * 4).ok_or("probe overflow")?)
        .ok_or("probe body truncated")?;
    *offset += n * 4;
    let mut words = [0; MAX_WORDS];
    for (word, bytes) in words.iter_mut().zip(data.chunks_exact(4)) {
        *word = u32::from_le_bytes(bytes.try_into().unwrap());
    }
    Ok((tag, words, n))
}
pub fn execute(
    input: &[u8],
    mut emit: impl FnMut(u32, Checkpoint),
    mut distance: impl FnMut(u32, u32),
) -> Result<Report, Error> {
    if input.len() > MAX_BYTES || input.get(..8) != Some(MAGIC) {
        return Err("probe header/version/size".into());
    }
    let mut offset = 8;
    let mut pipeline = Pipeline::new();
    let mut case = None;
    let mut seen = 0u32;
    let mut report = Report {
        cases: 0,
        events: 0,
        checkpoints: 0,
        distances: 0,
    };
    while offset < input.len() {
        let (tag, w, n) = body(input, &mut offset)?;
        report.events += 1;
        if report.events > MAX_EVENTS {
            return Err("probe event bound".into());
        }
        if tag == BEGIN {
            if n != 1 || !(1..=16).contains(&w[0]) || case.is_some() || seen & (1 << w[0]) != 0 {
                return Err("probe case boundary".into());
            }
            seen |= 1 << w[0];
            case = Some(w[0]);
            report.cases += 1;
            pipeline = Pipeline::new();
            continue;
        }
        let id = case.ok_or("probe event outside case")?;
        let mut checkpoint = |u: Update| {
            report.checkpoints += 1;
            emit(id, u.into());
        };
        match tag {
            CLOCK if n == 3 => pipeline.clock(time(&w), flag(w[2])?, &mut checkpoint),
            LABEL if n == 3 => pipeline.event(
                Event::Label {
                    time_us: time(&w),
                    label: Label::from_word(w[2]).ok_or("probe label")?,
                },
                &mut checkpoint,
            ),
            TRAIN_STOP if n == 3 => pipeline.event(
                Event::TrainingStop {
                    time_us: time(&w),
                    success: flag(w[2])?,
                },
                &mut checkpoint,
            ),
            SOURCE if n >= 12 => {
                let columns = w[10] as usize;
                let unavailable = flag(w[11])?;
                if columns > COLUMNS || n != 12 + columns || (!unavailable && columns < 2) {
                    return Err("probe profile shape".into());
                }
                let mut values = [0.0; COLUMNS];
                for (value, word) in values.iter_mut().zip(&w[12..n]) {
                    *value = f32::from_bits(*word);
                }
                pipeline.source(
                    Feature {
                        sequence: w[2],
                        oldest_us: time(&w[4..]),
                        newest_us: time(&w[6..]),
                        templates: 0,
                        training_complete: flag(w[8])?,
                        gain_available: flag(w[9])?,
                        wander: 0.0,
                    },
                    w[3],
                    Profile::copy(&values[..columns]),
                    time(&w),
                    unavailable,
                    &mut checkpoint,
                );
            }
            DISTANCE if n >= 6 => {
                let cols = w[0] as usize;
                if !(2..=temporal::COLUMNS).contains(&cols) || n != 2 + 2 * cols {
                    return Err("probe distance shape".into());
                }
                let mut a = [0.0; temporal::COLUMNS];
                let mut b = a;
                for i in 0..cols {
                    a[i] = f32::from_bits(w[2 + i]);
                    b[i] = f32::from_bits(w[2 + cols + i]);
                }
                let actual = temporal::distance(&a[..cols], &b[..cols])
                    .map_err(|_| "probe unsupported native comparison")?
                    .to_bits();
                if actual != w[1] {
                    return Err(Error::Arithmetic {
                        case: id,
                        comparison: report.distances + 1,
                        expected: w[1],
                        actual,
                    });
                }
                report.distances += 1;
                distance(id, actual);
            }
            END if n == 2 => {
                pipeline.event(Event::End(time(&w)), &mut checkpoint);
                case = None;
            }
            _ => return Err("probe record schema".into()),
        }
    }
    if case.is_some() || report.cases == 0 {
        return Err("probe missing case end".into());
    }
    Ok(report)
}
pub fn append(out: &mut impl Write, tag: u32, words: &[u32]) -> io::Result<()> {
    out.write_all(&tag.to_le_bytes())?;
    out.write_all(&(words.len() as u32).to_le_bytes())?;
    for word in words {
        out.write_all(&word.to_le_bytes())?;
    }
    Ok(())
}
pub fn run(input: &[u8], out: &mut impl Write) -> Result<Report, Box<dyn std::error::Error>> {
    // Reject invalid/truncated inputs before emitting a plausible result prefix.
    execute(input, |_, _| {}, |_, _| {})?;
    writeln!(
        out,
        "CSI_POLICY_BEGIN v=1 radio=0 virtual_time=1 physical_detector_admitted=0"
    )?;
    let mut failure = None;
    let mut write = |line: &[u8]| {
        if failure.is_none() {
            failure = out.write_all(line).err();
        }
    };
    // One ordered sink preserves comparison/checkpoint order without global I/O.
    let lines = std::cell::RefCell::new(&mut write);
    let r = execute(
        input,
        |case, c| {
            let mut buffer = [0u8; 384];
            let mut line = io::Cursor::new(buffer.as_mut_slice());
            write!(
                &mut line,
                "CSI_POLICY_CHECK case={case:08x} time={:016x} bits=",
                c.time_us
            )
            .unwrap();
            for word in c.words {
                write!(&mut line, "{word:08x}").unwrap();
            }
            writeln!(&mut line).unwrap();
            let n = line.position() as usize;
            lines.borrow_mut()(&buffer[..n]);
        },
        |case, bits| {
            let mut buffer = [0u8; 96];
            let mut line = io::Cursor::new(buffer.as_mut_slice());
            writeln!(
                &mut line,
                "CSI_POLICY_DISTANCE case={case:08x} bits={bits:08x}"
            )
            .unwrap();
            let n = line.position() as usize;
            lines.borrow_mut()(&buffer[..n]);
        },
    )?;
    if let Some(error) = failure {
        return Err(error.into());
    }
    writeln!(out,"CSI_POLICY_END cases={} events={} checkpoints={} distances={} physical_detector_admitted=0",r.cases,r.events,r.checkpoints,r.distances)?;
    out.flush()?;
    Ok(r)
}

//! The actual bounded FFmpeg segment-list protocol; no file or process operations.
use crate::store::Error;
use std::path::{Path, PathBuf};

pub(crate) struct SegmentRecord {
    pub path: PathBuf,
    pub duration_ms: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Failure {
    Ended,
    Line,
    Encoding,
    Fields,
    Path,
    NonFiniteTime,
    NonIncreasingTime,
    ShortInterval,
    LongInterval,
}
impl Failure {
    pub(crate) fn stage(self) -> &'static str {
        match self {
            Self::Ended => "segment input ended",
            Self::Line => "segment line refused",
            Self::Encoding => "segment encoding refused",
            Self::Fields => "segment fields refused",
            Self::Path => "segment path refused",
            Self::NonFiniteTime => "segment time refused",
            Self::NonIncreasingTime => "segment interval nonincreasing",
            Self::ShortInterval => "segment interval too short",
            Self::LongInterval => "segment interval too long",
        }
    }
}
impl From<Failure> for Error {
    fn from(value: Failure) -> Self {
        match value {
            Failure::Ended | Failure::Line => Self::Io,
            _ => Self::Corrupt,
        }
    }
}
pub(crate) fn segment(line: &[u8], directory: &Path) -> Result<SegmentRecord, Failure> {
    if line.is_empty() {
        return Err(Failure::Ended);
    }
    if line.len() > 1024 || line.last() != Some(&b'\n') {
        return Err(Failure::Line);
    }
    let line = std::str::from_utf8(line)
        .map_err(|_| Failure::Encoding)?
        .trim();
    let mut fields = line.rsplitn(3, ',');
    let end: f64 = fields
        .next()
        .ok_or(Failure::Fields)?
        .parse()
        .map_err(|_| Failure::Fields)?;
    let start: f64 = fields
        .next()
        .ok_or(Failure::Fields)?
        .parse()
        .map_err(|_| Failure::Fields)?;
    let path = PathBuf::from(fields.next().ok_or(Failure::Fields)?.trim_matches('"'));
    let path = if path.components().count() == 1 {
        directory.join(path)
    } else {
        path
    };
    if path.parent() != Some(directory)
        || !path.file_name().and_then(|p| p.to_str()).is_some_and(|p| {
            p.starts_with("part-")
                && p.ends_with(".ts")
                && p.len() == 14
                && p[5..11].bytes().all(|b| b.is_ascii_digit())
        })
    {
        return Err(Failure::Path);
    }
    if !start.is_finite() || !end.is_finite() {
        return Err(Failure::NonFiniteTime);
    }
    let duration = end - start;
    if !(0.01..=10.0).contains(&duration) {
        return Err(if duration <= 0.0 {
            Failure::NonIncreasingTime
        } else if duration < 0.01 {
            Failure::ShortInterval
        } else {
            Failure::LongInterval
        });
    }
    Ok(SegmentRecord {
        path,
        duration_ms: ((end - start) * 1000.0) as u64,
    })
}

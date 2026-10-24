//! The actual bounded FFmpeg segment-list protocol; no file or process operations.
use crate::store::Error;
use std::path::{Path, PathBuf};

pub(crate) struct SegmentRecord {
    pub path: PathBuf,
    pub duration_ms: u64,
}
pub(crate) fn segment(line: &[u8], directory: &Path) -> Result<SegmentRecord, Error> {
    if line.is_empty() || line.len() > 1024 || line.last() != Some(&b'\n') {
        return Err(Error::Io);
    }
    let line = std::str::from_utf8(line)
        .map_err(|_| Error::Corrupt)?
        .trim();
    let mut fields = line.rsplitn(3, ',');
    let end: f64 = fields
        .next()
        .ok_or(Error::Corrupt)?
        .parse()
        .map_err(|_| Error::Corrupt)?;
    let start: f64 = fields
        .next()
        .ok_or(Error::Corrupt)?
        .parse()
        .map_err(|_| Error::Corrupt)?;
    let path = PathBuf::from(fields.next().ok_or(Error::Corrupt)?.trim_matches('"'));
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
        || !start.is_finite()
        || !end.is_finite()
        || !(0.01..=10.0).contains(&(end - start))
    {
        return Err(Error::Corrupt);
    }
    Ok(SegmentRecord {
        path,
        duration_ms: ((end - start) * 1000.0) as u64,
    })
}

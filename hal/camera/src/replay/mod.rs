//! Explicit bounded raw-frame replay, enabled by the `replay` Cargo feature.
//!
//! Requires Rust `std` file I/O, not a camera profile or a hardware backend.
//! File loading is setup work; frame polling uses the shared in-memory source.
#![forbid(unsafe_code)]

mod source;
use crate::{Camera, Format};
pub use source::ReplayCamera;
use std::io;

pub fn open(
    source: &str,
    format: Format,
    period_ms: u64,
    max_source_bytes: usize,
) -> io::Result<(impl Camera, u64)> {
    let camera = ReplayCamera::load(source, format, period_ms, max_source_bytes)?;
    let frames = u64::try_from(camera.frame_count())
        .map_err(|_| io::Error::other("replay frame count overflow"))?;
    Ok((camera, frames))
}

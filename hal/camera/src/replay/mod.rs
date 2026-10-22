//! Bounded raw-frame replay for desktop hosts; not a physical camera driver.
#![forbid(unsafe_code)]

mod source;
use crate::{Camera, Format};
pub use source::ReplayCamera;
use std::io;

pub fn camera(
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

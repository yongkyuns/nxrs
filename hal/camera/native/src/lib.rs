//! Bounded raw-frame replay for desktop hosts; not a physical camera driver.
#![forbid(unsafe_code)]
#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
compile_error!("native camera replay supports Linux, macOS and Windows only");

mod replay;
pub use replay::ReplayCamera;
use std::io;
use camera_api::{Camera, Format};

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

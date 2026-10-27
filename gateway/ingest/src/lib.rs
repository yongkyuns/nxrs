//! Single-source nxrs MJPEG ingest. This is a separate process, not the control API.
#![deny(unsafe_code)]
pub mod config;
mod detections;
mod video;
pub mod protocol;
pub mod runner;
mod tls;

/// Validate immutable credentials and policy without opening a network or encoder.
pub fn check(config: &config::Config, require_tls: bool) -> Result<()> {
    config.validate()?;
    if require_tls && config.camera_tls.is_none() { return Err(Error::Configuration); }
    config.token()?;
    tls::Prepared::load(config)?;
    Ok(())
}

/// Fixed categories only: neither remote bytes nor credential-bearing argv enter logs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Configuration,
    Authentication,
    Io,
    Protocol,
    Frame,
    Stale,
    Timeout,
    Encoder,
    Cleanup,
    Stopped,
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Self::Io
    }
}
pub type Result<T> = std::result::Result<T, Error>;

#[cfg(feature = "publisher-sdk")]
mod media;
#[cfg(feature = "publisher-sdk")]
pub use media::{publish, read_rtsp, Failure as MediaError};

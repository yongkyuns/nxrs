//! Single-source nxrs MJPEG ingest. This is a separate process, not the control API.
#![forbid(unsafe_code)]
pub mod config;
mod detections;
mod video;
pub mod protocol;
pub mod runner;

/// Fixed categories only: neither remote bytes nor credential-bearing argv enter logs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Configuration,
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

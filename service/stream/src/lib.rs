//! Bounded multipart camera streaming and its versioned A/V contract.
#![cfg_attr(not(feature = "server"), no_std)]
#![forbid(unsafe_code)]

pub mod wire;

#[cfg(feature = "server")]
mod runtime;
#[cfg(feature = "server")]
pub use runtime::{
    CompletedFrame, Failure, HttpSession, Metadata, MjpegSession, OfferResult, Options,
    PollProgress, Progress, RejectReason, Server, ServerStats, SessionError, SessionStats,
    MAX_METADATA_BYTES,
};

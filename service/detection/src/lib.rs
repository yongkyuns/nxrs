//! Person detection sessions and their versioned publication contract.
#![cfg_attr(not(any(feature = "inference", feature = "wire")), no_std)]
#![forbid(unsafe_code)]

#[cfg(feature = "inference")]
mod inference;
#[cfg(feature = "inference")]
pub use inference::{
    Bounds, Detection, Detections, Error, Input, Observation, Policy, Score, Session, Snapshot,
    WorkerOptions, WorkerStats,
};

#[cfg(feature = "wire")]
pub mod wire;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[cfg_attr(
    feature = "wire",
    derive(serde::Serialize, serde::Deserialize),
    serde(rename_all = "lowercase")
)]
pub enum Presence {
    #[default]
    Unavailable,
    Clear,
    Person,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[cfg_attr(
    feature = "wire",
    derive(serde::Serialize, serde::Deserialize),
    serde(rename_all = "snake_case")
)]
pub enum EventKind {
    PersonEntered,
    PersonLeft,
    Unavailable,
}

pub const EVENT_HISTORY: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[cfg_attr(
    feature = "wire",
    derive(serde::Serialize, serde::Deserialize),
    serde(deny_unknown_fields)
)]
pub struct Event {
    pub id: u64,
    pub at_ms: u64,
    pub kind: EventKind,
}

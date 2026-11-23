//! Detection states shared by the policy and the versioned camera protocol.
#![cfg_attr(not(feature = "json"), no_std)]
#![forbid(unsafe_code)]

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[cfg_attr(
    feature = "json",
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
    feature = "json",
    derive(serde::Serialize, serde::Deserialize),
    serde(rename_all = "snake_case")
)]
pub enum EventKind {
    PersonEntered,
    PersonLeft,
    Unavailable,
}

#[cfg(feature = "json")]
mod protocol;
#[cfg(feature = "json")]
pub use protocol::{
    Bounded, Bounds, Class, Event, Object, Observation, Snapshot, Source, Update, Version,
    VideoFrame, EVENT_HISTORY, MAX_AGE_MS, MAX_SOURCE_BYTES, MAX_VIDEO_BYTES, METADATA_TYPE,
    SOURCE_TIMEOUT_MS, VIDEO_SEI_UUID,
};

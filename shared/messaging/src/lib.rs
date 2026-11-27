//! Layer-neutral typed delivery and optional task-context communication.
//!
//! `channels` supplies bounded event queues; the independent `latest` feature
//! supplies fixed-reader, coalescing snapshots through `messaging::latest`.
//! `pooled` supplies retained, shared-capacity delivery of scalar records from
//! three exclusive sources. With no features, delivery contracts need only
//! core. `latest` uses `no_std + alloc`; `channels` and `pooled` use `std`.
#![no_std]
#![forbid(unsafe_code)]

#[cfg(any(feature = "channels", feature = "pooled"))]
extern crate std;
#[cfg(feature = "channels")]
mod channel;
#[cfg(feature = "channels")]
pub use channel::{bounded, Inbox, Sender};

#[cfg(feature = "latest")]
pub mod latest;

#[cfg(feature = "pooled")]
pub mod pooled;

/// Rejection returns the original owned value to its producer.
#[derive(Debug, PartialEq, Eq)]
pub struct Rejected<T, E> {
    pub value: T,
    pub error: E,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeliveryError {
    Full,
    Closed,
}

/// Callback admission result. Callbacks enqueue in task context; they must not
/// execute receiver handlers inline or wait for queue capacity. Producers retain
/// important rejected events in bounded storage or expose a sticky failure.
pub type DeliveryResult<T> = Result<(), Rejected<T, DeliveryError>>;

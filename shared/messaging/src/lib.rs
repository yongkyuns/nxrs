//! Layer-neutral typed delivery and optional bounded task-context channels.
#![no_std]
#![forbid(unsafe_code)]

#[cfg(feature = "channels")]
extern crate std;
#[cfg(feature = "channels")]
mod channel;
#[cfg(feature = "channels")]
pub use channel::{bounded, Inbox, Sender};

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

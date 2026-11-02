//! Shared radio request identities and bounded payload storage.
#![no_std]
#![forbid(unsafe_code)]

mod events;
pub use events::{BoundedBytes, OperationId, RequestId, SessionId};

//! Shared radio event-delivery protocol, without device errors or providers.
#![no_std]
#![forbid(unsafe_code)]

mod events;
pub use events::{
    BoundedBytes, DeliveryError, EventSink, OperationId, Rejected, RequestId, SessionId,
};

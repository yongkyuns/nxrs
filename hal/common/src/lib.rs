//! Shared contract values, without providers, OS types or runtime policy.
#![no_std]
#![forbid(unsafe_code)]

mod events;
pub use events::{
    BoundedBytes, DeliveryError, EventSink, OperationId, Rejected, RequestId, SessionId,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeviceError {
    Unsupported,
    Busy,
    Timeout,
    Full,
    Io,
    InvalidData,
}

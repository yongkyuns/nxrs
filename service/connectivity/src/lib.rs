//! Bounded publication and secure connections driven by the caller's wait loop.
#![forbid(unsafe_code)]

#[cfg(feature = "ble")]
pub mod ble;
#[cfg(feature = "cellular")]
pub mod cellular;
mod ip;
mod publication;

#[cfg(feature = "pinned-tls")]
pub use ip::Dial;
pub use ip::Endpoint;
pub use publication::Publisher;

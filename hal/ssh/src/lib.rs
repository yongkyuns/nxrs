//! Owns one native maintenance shell. Authentication and the SSH protocol
//! stay in the OS utility; this API only starts and joins its task.
#![deny(unsafe_code)]

mod platform;
pub use platform::{start, Session};

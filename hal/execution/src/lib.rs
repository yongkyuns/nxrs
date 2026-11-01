//! Calling-thread scheduling. std::thread still owns creation, joins and waits.
#![no_std]
#![deny(unsafe_code)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Priority {
    Background,
    Normal,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Io,
}
mod platform;
pub use platform::configure_current;

//! Device TLS credential ownership. Unbound builds acquire no SDK or entropy.
#![no_std]
#![deny(unsafe_code)]
extern crate std;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    Entropy,
    Poisoned,
    InvalidInput,
    Capacity,
    Crypto,
    Authentication,
    Time,
    Io,
}

#[cfg_attr(
    not(any(platform = "linux", platform = "macos", platform = "nuttx")),
    allow(dead_code)
)]
mod api;
pub use api::{CertificatePolicy, Certificates, TimeLease};
mod platform;
#[cfg(any(platform = "linux", platform = "macos", platform = "nuttx"))]
#[allow(unsafe_code)]
mod sdk;
pub use platform::{open, Connection, Identity};

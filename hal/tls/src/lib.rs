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

#[cfg_attr(not(hal_enabled), allow(dead_code))]
mod api;
pub use api::{CertificatePolicy, Certificates, TimeLease};
mod platform;
#[cfg(hal_enabled)]
#[allow(unsafe_code)]
mod sdk;
pub use platform::{open, Connection, Identity};

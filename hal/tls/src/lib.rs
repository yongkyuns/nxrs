//! Device TLS credential ownership. Unbound builds acquire no SDK or entropy.
#![no_std]
#![deny(unsafe_code)]
#[cfg(hal_enabled)]
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
}

#[cfg(hal_enabled)]
mod platform;
#[cfg(hal_enabled)]
#[allow(unsafe_code)]
mod sdk;
#[cfg(hal_enabled)]
pub use platform::open;
#[cfg(hal_enabled)]
pub use sdk::Identity;

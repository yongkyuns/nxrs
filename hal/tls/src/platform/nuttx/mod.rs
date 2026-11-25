#![allow(unsafe_code)]
mod ffi;
use self::ffi::{tls_entropy_open_flags, tls_rng_admitted};
use crate::entropy;
#[cfg(test)]
pub(crate) use entropy::Source;

pub fn open() -> Result<crate::Identity, crate::Error> {
    // SAFETY: a target-header constant; no hardware mutation or retained pointer.
    if !unsafe { tls_rng_admitted() } {
        return Err(crate::Error::Unsupported);
    }
    // SAFETY: O_NONBLOCK from the selected target's fcntl.h.
    let flags = unsafe { tls_entropy_open_flags() };
    crate::Identity::generate(entropy::Source::open("/dev/random", flags)?)
}

pub use crate::sdk::{Connection, Identity};

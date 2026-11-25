#![allow(unsafe_code)]
use crate::sdk::{Entropy, EntropyUnavailable};
mod ffi;
use self::ffi::getentropy;
pub(crate) struct Source;
impl Entropy for Source {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        // SAFETY: writable, at most256 bytes, matching the native API limit.
        if unsafe { getentropy(output.as_mut_ptr().cast(), output.len()) } == 0 {
            Ok(())
        } else {
            Err(EntropyUnavailable)
        }
    }
}
pub fn open() -> Result<crate::Identity, crate::Error> {
    crate::Identity::generate(Source)
}

pub use crate::sdk::{Connection, Identity};

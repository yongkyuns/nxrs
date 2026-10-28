#![allow(unsafe_code)]
use crate::sdk::{Entropy, EntropyUnavailable};
use std::ffi::c_void;
unsafe extern "C" {
    fn getrandom(buffer: *mut c_void, size: usize, flags: u32) -> isize;
    fn tls_nonblocking_entropy_flag() -> u32;
}
pub(crate) struct Source;
impl Entropy for Source {
    fn fill(&mut self, output: &mut [u8]) -> Result<(), EntropyUnavailable> {
        // SAFETY: SDK admission bounds output to256 writable bytes. Actual
        // Linux headers supply GRND_NONBLOCK; unseeded/error/partial results fail.
        let n = unsafe {
            getrandom(
                output.as_mut_ptr().cast(),
                output.len(),
                tls_nonblocking_entropy_flag(),
            )
        };
        if n == output.len() as isize {
            Ok(())
        } else {
            Err(EntropyUnavailable)
        }
    }
}
pub fn open() -> Result<crate::Identity, crate::Error> {
    crate::Identity::generate(Source)
}

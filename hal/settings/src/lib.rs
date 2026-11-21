//! Settings storage for small persistent application records.
//!
//! Applications define the keys and record formats. On NuttX, the profile
//! binds this API to the named record store at `/dev/config`.
#![no_std]
#![deny(unsafe_code)]

#[cfg(any(platform = "nuttx", test))]
extern crate std;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    InvalidInput,
    BufferTooSmall,
    Full,
    Io,
}

/// Keys contain 1–31 ASCII letters, digits, '.', '_' or '-'. Values are
/// nonempty. Persistence does not imply encryption or secure deletion.
pub trait Store {
    /// Only a missing key returns None. On error, discard the output buffer.
    fn get(&mut self, key: &str, output: &mut [u8]) -> Result<Option<usize>, Error>;
    fn set(&mut self, key: &str, value: &[u8]) -> Result<(), Error>;
    /// Logical deletion; previous values may remain in physical storage.
    fn remove(&mut self, key: &str) -> Result<(), Error>;
}

mod platform;
pub use platform::open;

#[cfg(test)]
mod test_support;

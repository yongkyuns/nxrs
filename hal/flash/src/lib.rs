//! Direct access to a dedicated raw NOR partition. Use `std::fs` for files.
#![deny(unsafe_code)]

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Unsupported,
    Busy,
    OutOfBounds,
    Unaligned,
    Io,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Geometry {
    pub capacity: u32,
    pub program_size: u32,
    pub erase_size: u32,
}

/// Exclusive within Nxrs. A failed cache invalidation requires reopening.
pub trait Partition {
    fn geometry(&self) -> Geometry;

    /// Read at a partition-relative byte offset. Discard the buffer on error.
    fn read(&mut self, offset: u32, output: &mut [u8]) -> Result<(), Error>;

    /// Program whole aligned blocks in previously erased space. Erase before
    /// programming a block again. On error the range may be partially written.
    fn program(&mut self, offset: u32, data: &[u8]) -> Result<(), Error>;

    /// Erase whole aligned sectors to 0xff. On error the range may be partially erased.
    fn erase(&mut self, offset: u32, length: u32) -> Result<(), Error>;
}

mod platform;
pub use platform::open;

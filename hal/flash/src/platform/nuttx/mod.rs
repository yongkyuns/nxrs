use crate::{Error, Geometry, Partition};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsFd;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::atomic::{AtomicBool, Ordering};

#[allow(unsafe_code)]
mod ffi;

static TAKEN: AtomicBool = AtomicBool::new(false);

struct Lease;

impl Drop for Lease {
    fn drop(&mut self) {
        TAKEN.store(false, Ordering::Release);
    }
}

struct NorPartition {
    file: File,
    geometry: Geometry,
    cache_valid: bool,
    _lease: Lease,
}

pub fn open() -> Result<impl Partition, Error> {
    if TAKEN.swap(true, Ordering::AcqRel) {
        return Err(Error::Busy);
    }
    let lease = Lease;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(ffi::direct_flags())
        .open(env!("FLASH_DEVICE"))
        .map_err(|_| Error::Io)?;
    let fd = file.as_fd();
    let (program_size, erase_size, sectors) = ffi::geometry(fd)?;
    let capacity = erase_size.checked_mul(sectors).ok_or(Error::Unsupported)?;
    // FTL assumes a power-of-two ratio. Keep seeks within signed NuttX off_t.
    if !program_size.is_power_of_two()
        || !erase_size.is_power_of_two()
        || erase_size < program_size
        || capacity == 0
        || capacity > i32::MAX as u32
        || ffi::erased_byte(fd)? != 0xff
    {
        return Err(Error::Unsupported);
    }
    Ok(NorPartition {
        file,
        geometry: Geometry {
            capacity,
            program_size,
            erase_size,
        },
        cache_valid: true,
        _lease: lease,
    })
}

impl NorPartition {
    fn range(&self, offset: u32, length: usize, unit: u32) -> Result<(), Error> {
        if !self.cache_valid {
            return Err(Error::Io);
        }
        if offset > self.geometry.capacity || length > (self.geometry.capacity - offset) as usize {
            return Err(Error::OutOfBounds);
        }
        if !offset.is_multiple_of(unit) || !length.is_multiple_of(unit as usize) {
            return Err(Error::Unaligned);
        }
        Ok(())
    }

    fn seek(&mut self, offset: u32) -> Result<(), Error> {
        match self.file.seek(SeekFrom::Start(offset.into())) {
            Ok(position) if position == u64::from(offset) => Ok(()),
            _ => Err(Error::Io),
        }
    }

    fn invalidate_cache(&mut self) -> Result<(), Error> {
        let result = ffi::discard(self.file.as_fd());
        self.cache_valid = result.is_ok();
        result
    }
}

impl Partition for NorPartition {
    fn geometry(&self) -> Geometry {
        self.geometry
    }

    fn read(&mut self, offset: u32, output: &mut [u8]) -> Result<(), Error> {
        self.range(offset, output.len(), 1)?;
        if output.is_empty() {
            return Ok(());
        }
        self.seek(offset)?;
        self.file.read_exact(output).map_err(|_| Error::Io)
    }

    fn program(&mut self, offset: u32, data: &[u8]) -> Result<(), Error> {
        self.range(offset, data.len(), self.geometry.program_size)?;
        if data.is_empty() {
            return Ok(());
        }
        self.seek(offset)?;
        let written = self.file.write(data);
        // The BCH proxy may still hold a sector read before programming.
        let discarded = self.invalidate_cache();
        if !matches!(written, Ok(n) if n == data.len()) {
            return Err(Error::Io);
        }
        discarded
    }

    fn erase(&mut self, offset: u32, length: u32) -> Result<(), Error> {
        let unit = self.geometry.erase_size;
        self.range(offset, length as usize, unit)?;
        if length == 0 {
            return Ok(());
        }
        let erased = ffi::erase(self.file.as_fd(), offset / unit, length / unit);
        let discarded = self.invalidate_cache();
        erased.and(discarded)
    }
}

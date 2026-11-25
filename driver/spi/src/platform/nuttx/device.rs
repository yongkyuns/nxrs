use super::{ffi, Config, Error};
use embedded_hal::spi::{ErrorType, Operation, SpiDevice};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
};

const MAX_BYTES: usize = 256;
pub struct Device {
    file: File,
    chip_select: u16,
    frequency: u32,
    mode: u8,
}
impl Device {
    pub fn open(config: Config<'_>) -> Result<Self, Error> {
        if config.path.is_empty() || config.frequency == 0 || config.mode > 3 {
            return Err(Error::InvalidConfig);
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(config.path)
            .map_err(|_| Error::Io)?;
        Ok(Self {
            file,
            chip_select: config.chip_select,
            frequency: config.frequency,
            mode: config.mode,
        })
    }
}
impl ErrorType for Device {
    type Error = Error;
}
impl SpiDevice for Device {
    fn transaction(&mut self, operations: &mut [Operation<'_, u8>]) -> Result<(), Error> {
        let mut tx = [0; MAX_BYTES];
        let mut used = 0;
        // Validate the entire transaction before I/O. Flattening preserves one
        // CS assertion and one NuttX bus lock without exposing C message layouts.
        for op in operations.iter() {
            let (write, len): (&[u8], usize) = match op {
                Operation::Read(read) => (&[], read.len()),
                Operation::Write(write) => (write, write.len()),
                Operation::Transfer(read, write) => (write, read.len().max(write.len())),
                Operation::TransferInPlace(bytes) => (bytes, bytes.len()),
                Operation::DelayNs(_) => return Err(Error::Unsupported),
            };
            if len > MAX_BYTES - used {
                return Err(Error::Unsupported);
            }
            tx[used..used + write.len()].copy_from_slice(write);
            used += len;
        }
        if used == 0 {
            return Ok(());
        }
        let mut rx = [0; MAX_BYTES];
        ffi::transfer(
            self.file.as_raw_fd(),
            self.chip_select,
            self.frequency,
            self.mode,
            &tx[..used],
            &mut rx[..used],
        )
        .map_err(|_| Error::Io)?;
        let mut offset = 0;
        for op in operations {
            let len = match op {
                Operation::Read(read) | Operation::TransferInPlace(read) => {
                    read.copy_from_slice(&rx[offset..offset + read.len()]);
                    read.len()
                }
                Operation::Write(write) => write.len(),
                Operation::Transfer(read, write) => {
                    read.copy_from_slice(&rx[offset..offset + read.len()]);
                    read.len().max(write.len())
                }
                Operation::DelayNs(_) => unreachable!("validated before I/O"),
            };
            offset += len;
        }
        Ok(())
    }
}

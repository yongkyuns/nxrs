//! Owned NuttX controller descriptor; no sensor protocol or acquisition policy.
use super::{ffi, Config, Error};
use embedded_hal::i2c::{ErrorType, I2c, Operation};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
};

pub struct Bus {
    file: File,
    frequency: u32,
}

impl Bus {
    pub fn open(config: Config<'_>) -> Result<Self, Error> {
        if config.frequency == 0 || config.path.is_empty() {
            return Err(Error::InvalidConfig);
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(config.path)
            .map_err(|_| Error::Io)?;
        Ok(Self {
            file,
            frequency: config.frequency,
        })
    }
}

impl ErrorType for Bus {
    type Error = Error;
}

impl I2c for Bus {
    fn transaction(
        &mut self,
        address: u8,
        operations: &mut [Operation<'_>],
    ) -> Result<(), Self::Error> {
        if address > 0x7f {
            return Err(Error::InvalidConfig);
        }
        // The target bridge supports one write, one read, or an atomic
        // write/read pair. Reject other sequences before issuing any I/O.
        let mut no_read = [];
        let (write, read): (&[u8], &mut [u8]) = match operations {
            [Operation::Write(bytes)] => (bytes, &mut no_read),
            [Operation::Read(bytes)] => (&[], bytes),
            [Operation::Write(write), Operation::Read(read)] => (write, read),
            [] => return Ok(()),
            _ => return Err(Error::Unsupported),
        };
        if write.len() > 64 || read.len() > 64 {
            return Err(Error::Unsupported);
        }
        ffi::transfer(self.file.as_raw_fd(), address, self.frequency, write, read)
            .map_err(|_| Error::Io)
    }
}

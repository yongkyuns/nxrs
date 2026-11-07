//! Owned NuttX I2C descriptor for the IMU's bounded register transactions.
use super::{ffi, trace::Trace};
use crate::DeviceError;
use embedded_hal::i2c::{ErrorKind, ErrorType, I2c, Operation};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
};

pub(super) struct Bus {
    file: File,
    frequency: u32,
    trace: Trace,
}

impl Bus {
    pub(super) fn open(path: &str, frequency: u32, trace: Trace) -> Result<Self, DeviceError> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(|_| DeviceError::Io)?;
        Ok(Self {
            file,
            frequency,
            trace,
        })
    }
}

impl ErrorType for Bus {
    type Error = ErrorKind;
}

impl I2c for Bus {
    fn transaction(
        &mut self,
        address: u8,
        operations: &mut [Operation<'_>],
    ) -> Result<(), Self::Error> {
        // The target bridge supports one write, one read, or an atomic
        // write/read pair. Reject other sequences before issuing any I/O.
        let mut no_read = [];
        let (write, read): (&[u8], &mut [u8]) = match operations {
            [Operation::Write(bytes)] => (bytes, &mut no_read),
            [Operation::Read(bytes)] => (&[], bytes),
            [Operation::Write(write), Operation::Read(read)] => (write, read),
            [] => return Ok(()),
            _ => return Err(ErrorKind::Other),
        };
        if write.len() > 64 || read.len() > 64 {
            return Err(ErrorKind::Other);
        }
        self.trace
            .ioctl(|| ffi::transfer(self.file.as_raw_fd(), address, self.frequency, write, read))
            .map_err(|_| ErrorKind::Other)
    }
}

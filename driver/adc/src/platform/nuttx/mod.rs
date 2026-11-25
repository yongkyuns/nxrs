use crate::Sample;
use std::{
    fs::{File, OpenOptions},
    io::{self, Read},
    os::{fd::AsFd, unix::fs::OpenOptionsExt},
};

#[allow(unsafe_code)]
mod ffi;

/// A BSP-configured ADC endpoint. Use one reader per device.
pub struct Adc(File);

impl Adc {
    /// Open without waiting for samples. An empty FIFO reports `WouldBlock`.
    pub fn open(path: &str) -> io::Result<Self> {
        OpenOptions::new()
            .read(true)
            .custom_flags(ffi::nonblocking())
            .open(path)
            .map(Self)
    }

    /// Read one complete sample from a streaming or previously triggered ADC.
    pub fn read(&mut self) -> io::Result<Sample> {
        // The NuttX upper half chooses the format from the buffer length.
        // A single 5-byte read yields one channel byte and a native i32.
        let mut record = [0; 5];
        if self.0.read(&mut record)? != record.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "short ADC sample",
            ));
        }
        let [channel, a, b, c, d] = record;
        Ok(Sample {
            channel,
            value: i32::from_ne_bytes([a, b, c, d]),
        })
    }

    /// Discard queued data and trigger a new scan. Read each configured
    /// channel through `read`. Only software-triggered devices support this.
    pub fn trigger(&mut self) -> io::Result<()> {
        let fd = self.0.as_fd();
        ffi::reset_fifo(fd)?;
        ffi::trigger(fd)
    }
}

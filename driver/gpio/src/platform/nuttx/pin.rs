use super::{ffi, Error};
use embedded_hal::digital::{ErrorType, InputPin, OutputPin};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, Write},
    os::fd::AsRawFd,
};

/// A BSP-configured digital input.
pub struct Input(File);

impl Input {
    pub fn open(path: &str) -> Result<Self, Error> {
        let file = File::open(path).map_err(|_| Error::Io)?;
        ffi::check_pin(file.as_raw_fd(), false).map_err(|_| Error::Unsupported)?;
        Ok(Self(file))
    }

    fn read(&mut self) -> Result<bool, Error> {
        self.0.rewind().map_err(|_| Error::Io)?;
        let mut level = [0];
        self.0.read_exact(&mut level).map_err(|_| Error::Io)?;
        match level[0] {
            b'0' => Ok(false),
            b'1' => Ok(true),
            _ => Err(Error::Io),
        }
    }
}

impl ErrorType for Input {
    type Error = Error;
}

impl InputPin for Input {
    fn is_high(&mut self) -> Result<bool, Error> {
        self.read()
    }

    fn is_low(&mut self) -> Result<bool, Error> {
        self.read().map(|high| !high)
    }
}

/// A BSP-configured output. The caller owns exclusive use of the endpoint.
/// Dropping the descriptor leaves the pin at its last level.
pub struct Output(File);

impl Output {
    pub fn open(path: &str) -> Result<Self, Error> {
        let file = OpenOptions::new()
            .write(true)
            .open(path)
            .map_err(|_| Error::Io)?;
        ffi::check_pin(file.as_raw_fd(), true).map_err(|_| Error::Unsupported)?;
        Ok(Self(file))
    }
}

impl ErrorType for Output {
    type Error = Error;
}

impl OutputPin for Output {
    fn set_low(&mut self) -> Result<(), Error> {
        self.0.write_all(b"0").map_err(|_| Error::Io)
    }

    fn set_high(&mut self) -> Result<(), Error> {
        self.0.write_all(b"1").map_err(|_| Error::Io)
    }
}

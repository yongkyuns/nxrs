use crate::{Clock, Error};
use std::fs::{File, OpenOptions};
use std::os::fd::AsFd;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[allow(unsafe_code)]
mod ffi;

// NuttX's default calendar conversion counts leap years in four-year cycles.
const LAST_SECOND: u64 = 4_102_444_799;

struct HardwareClock(File);

pub fn open() -> Result<impl Clock, Error> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(env!("RTC_DEVICE"))
        .map(HardwareClock)
        .map_err(|_| Error::Io)
}

impl Clock for HardwareClock {
    fn read(&self) -> Result<Option<SystemTime>, Error> {
        let fd = self.0.as_fd();
        if !ffi::is_set(fd)? {
            return Ok(None);
        }
        let seconds = ffi::read_seconds(fd)?;
        let seconds = u64::try_from(seconds).map_err(|_| Error::InvalidTime)?;
        if seconds > LAST_SECOND {
            return Err(Error::InvalidTime);
        }
        UNIX_EPOCH
            .checked_add(Duration::from_secs(seconds))
            .map(Some)
            .ok_or(Error::InvalidTime)
    }

    fn set(&mut self, time: SystemTime) -> Result<(), Error> {
        let seconds = time
            .duration_since(UNIX_EPOCH)
            .map_err(|_| Error::InvalidTime)?
            .as_secs();
        if seconds > LAST_SECOND {
            return Err(Error::InvalidTime);
        }
        ffi::set_seconds(self.0.as_fd(), seconds as i64)
    }
}

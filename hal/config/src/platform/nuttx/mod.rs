use crate::{Error, Store};
use std::{fs::File, os::fd::AsRawFd};

#[allow(unsafe_code)]
mod ffi {
    unsafe extern "C" {
        pub fn config_get(fd: i32, key: *const u8, data: *mut u8, length: *mut usize) -> i32;
        pub fn config_set(fd: i32, key: *const u8, data: *const u8, length: usize) -> i32;
        pub fn config_remove(fd: i32, key: *const u8) -> i32;
    }
}

pub(super) fn open() -> Result<impl Store, Error> {
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/config")
        .map(Nvs)
        .map_err(|_| Error::Io)
}

struct Nvs(File);

fn key(value: &str) -> Result<[u8; 32], Error> {
    if value.is_empty()
        || value.len() > 31
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Err(Error::InvalidInput);
    }
    let mut bytes = [0; 32];
    bytes[..value.len()].copy_from_slice(value.as_bytes());
    Ok(bytes)
}

fn result(status: i32) -> Result<(), Error> {
    match status {
        0 => Ok(()),
        // NuttX ENOSPC; the target-header binding supplies this classification.
        -1 => Err(Error::Full),
        _ => Err(Error::Io),
    }
}

#[allow(unsafe_code)]
impl Store for Nvs {
    fn get(&mut self, name: &str, output: &mut [u8]) -> Result<Option<usize>, Error> {
        let name = key(name)?;
        let mut length = output.len();
        // SAFETY: target-header ioctl translation receives bounded live buffers.
        let status = unsafe {
            ffi::config_get(
                self.0.as_raw_fd(),
                name.as_ptr(),
                output.as_mut_ptr(),
                &mut length,
            )
        };
        if status == 1 {
            return Ok(None);
        }
        result(status)?;
        // NVS can succeed after a partial read, which also skips its data CRC.
        if length > output.len() {
            return Err(Error::BufferTooSmall);
        }
        Ok(Some(length))
    }
    fn set(&mut self, name: &str, value: &[u8]) -> Result<(), Error> {
        let name = key(name)?;
        if value.is_empty() {
            return Err(Error::InvalidInput);
        }
        // SAFETY: the native call does not retain either input buffer.
        result(unsafe {
            ffi::config_set(
                self.0.as_raw_fd(),
                name.as_ptr(),
                value.as_ptr(),
                value.len(),
            )
        })
    }
    fn remove(&mut self, name: &str) -> Result<(), Error> {
        let name = key(name)?;
        // SAFETY: the native call does not retain this terminated name.
        result(unsafe { ffi::config_remove(self.0.as_raw_fd(), name.as_ptr()) })
    }
}

#[cfg(test)]
#[allow(unsafe_code)]
#[path = "../../../tests/nuttx/mod.rs"]
mod tests;

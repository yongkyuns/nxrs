//! NuttX UART resource ownership. Receiver protocol stays in ordinary Rust.
use crate::{DeviceError, Gnss};
use core::sync::atomic::{AtomicU8, Ordering};
use std::{
    fs::{File, OpenOptions},
    io::{self, Read},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[allow(unsafe_code)]
mod ffi;

static OWNED: AtomicU8 = AtomicU8::new(0);
struct Lease;
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = OWNED.compare_exchange(1, 0, Ordering::AcqRel, Ordering::Acquire);
    }
}
struct Port {
    file: File,
    saved: [u8; 64],
}
impl Drop for Port {
    fn drop(&mut self) {
        if ffi::restore(self.file.as_raw_fd(), &self.saved).is_err() {
            OWNED.store(2, Ordering::Release);
        }
    }
}
struct Serial {
    port: Port,
    _lease: Lease,
}
impl Read for Serial {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.port.file.read(bytes)
    }
}
pub fn open() -> Result<impl Gnss + Send + 'static, DeviceError> {
    OWNED
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|state| {
            if state == 1 {
                DeviceError::Busy
            } else {
                DeviceError::Io
            }
        })?;
    let lease = Lease;
    let path = option_env!("GNSS_DEVICE").ok_or(DeviceError::Unsupported)?;
    let baud = option_env!("GNSS_BAUD")
        .and_then(|b| b.parse::<u32>().ok())
        .ok_or(DeviceError::Unsupported)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(ffi::nonblocking_flag())
        .open(path)
        .map_err(|_| DeviceError::Io)?;
    let mut saved = [0; 64];
    ffi::capture(file.as_raw_fd(), &mut saved).map_err(|_| DeviceError::Io)?;
    let port = Port { file, saved };
    ffi::configure(port.file.as_raw_fd(), &port.saved, baud).map_err(|_| DeviceError::Io)?;
    // Port restores/closes before the lease can admit another receiver.
    Ok(crate::receiver::Receiver::new(Serial {
        port,
        _lease: lease,
    }))
}

//! Private target-header bindings for the product's execution and console setup.
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    os::fd::AsRawFd,
};

#[allow(unsafe_code)]
mod ffi {
    unsafe extern "C" {
        fn camera_network_initialize() -> i32;
        fn camera_worker_priority(priority: i32) -> i32;
        fn camera_console_ready(fd: i32) -> i32;
        fn camera_console_mode(fd: i32, fields: *mut u32, raw: i32) -> i32;
    }
    pub(crate) fn network() -> io::Result<()> {
        // SAFETY: synchronous SDK setup; no pointers/callbacks are retained.
        if unsafe { camera_network_initialize() } == 0 {
            Ok(())
        } else {
            Err(io::Error::other("network initialization failed"))
        }
    }
    pub(crate) fn priority(value: i32) -> io::Result<()> {
        // SAFETY: changes only the calling worker's scheduler parameters.
        if unsafe { camera_worker_priority(value) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }
    pub(crate) fn ready(fd: i32) -> io::Result<bool> {
        // SAFETY: target poll translation only observes this owner's live fd.
        match unsafe { camera_console_ready(fd) } {
            n if n < 0 => Err(io::Error::last_os_error()),
            n => Ok(n > 0),
        }
    }
    pub(crate) fn mode(fd: i32, saved: &mut [u32; 6], raw: bool) -> io::Result<()> {
        // SAFETY: six live scalar fields match the private target-header ABI.
        if unsafe { camera_console_mode(fd, saved.as_mut_ptr(), i32::from(raw)) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }
    use std::io;
}

pub(crate) use ffi::{network as initialize_network, priority};

pub(crate) struct Console {
    file: File,
    saved: [u32; 6],
}
impl Console {
    pub(crate) fn open() -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/ttyACM0")?;
        let mut saved = [0; 6];
        ffi::mode(file.as_raw_fd(), &mut saved, true)?;
        Ok(Self { file, saved })
    }
}
impl Drop for Console {
    fn drop(&mut self) {
        // Restore before the owned File is closed.
        let _ = ffi::mode(self.file.as_raw_fd(), &mut self.saved, false);
    }
}
impl Read for Console {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if !ffi::ready(self.file.as_raw_fd())? {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        self.file.read(bytes)
    }
}
impl Write for Console {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

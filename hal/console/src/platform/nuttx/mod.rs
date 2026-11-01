//! Private console acquisition and native termios/poll translation.
use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    os::fd::AsRawFd,
};

#[allow(unsafe_code)]
mod ffi;

pub fn open() -> io::Result<Console> {
    Console::open()
}

use crate::ownership::{Lease, Owner, Restore};
static TAKEN: core::sync::atomic::AtomicU8 = core::sync::atomic::AtomicU8::new(0);
struct Mode {
    file: File,
    saved: [u32; 6],
}
impl Restore for Mode {
    fn restore(&mut self) -> io::Result<()> {
        ffi::mode(self.file.as_raw_fd(), &mut self.saved, false)
    }
}
pub struct Console {
    owner: Owner<'static, Mode>,
}
impl Console {
    fn open() -> io::Result<Self> {
        let mut lease = Lease::acquire(&TAKEN)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(env!("CONSOLE_DEVICE"))?;
        let mut mode = Mode {
            file,
            saved: [0; 6],
        };
        lease.mark_uncertain();
        ffi::mode(mode.file.as_raw_fd(), &mut mode.saved, true)?;
        lease.restored();
        Ok(Self {
            owner: Owner::new(mode, lease),
        })
    }
}
impl Read for Console {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        if !ffi::ready(self.owner.resource.file.as_raw_fd())? {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        self.owner.resource.file.read(bytes)
    }
}
impl Write for Console {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.owner.resource.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.owner.resource.file.flush()
    }
}

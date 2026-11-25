//! Descriptor ownership and cancellation for the NuttX providers' native poll ABI.
use std::{
    fs::File,
    io::{self, Write},
    os::fd::{AsRawFd, RawFd},
};

pub struct Cancel {
    pub reader: std::sync::Arc<File>,
    writer: File,
}
impl Cancel {
    pub fn new(pipe: fn() -> io::Result<(File, File)>) -> io::Result<Self> {
        let (reader, writer) = pipe()?;
        Ok(Self {
            reader: std::sync::Arc::new(reader),
            writer,
        })
    }
}
impl crate::Wake for Cancel {
    fn wake(&self) -> io::Result<()> {
        match (&self.writer).write(&[1]) {
            Ok(1) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => Ok(()),
            Err(e) => Err(e),
            _ => Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "cancellation pipe",
            )),
        }
    }
}
pub fn wait(
    device: &File,
    cancel: &File,
    native: fn(RawFd, RawFd) -> io::Result<bool>,
) -> io::Result<bool> {
    loop {
        match native(device.as_raw_fd(), cancel.as_raw_fd()) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            result => return result,
        }
    }
}

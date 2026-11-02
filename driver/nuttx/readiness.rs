//! Descriptor ownership and cancellation for the NuttX providers' native poll ABI.
use std::{
    fs::File,
    io::{self, Write},
    os::fd::AsRawFd,
};

pub(crate) struct Cancel {
    pub(crate) reader: std::sync::Arc<File>,
    writer: File,
}
impl Cancel {
    pub(crate) fn new() -> io::Result<Self> {
        let (reader, writer) = super::ffi::event_pipe()?;
        Ok(Self {
            reader: std::sync::Arc::new(reader),
            writer,
        })
    }
}
impl crate::acquisition::Wake for Cancel {
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
pub(crate) fn wait(device: &File, cancel: &File) -> io::Result<bool> {
    loop {
        match super::ffi::wait(device.as_raw_fd(), cancel.as_raw_fd()) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            result => return result,
        }
    }
}

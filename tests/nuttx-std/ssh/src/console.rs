//! Owned, unbuffered maintenance handles. They do not share Rust's process-wide
//! stdin buffer or stdout/stderr locks with a remote Rust builtin.
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::BorrowedFd;

pub struct Console {
    pub input: File,
    pub output: File,
    pub error: File,
}

impl Console {
    pub fn stdio() -> io::Result<Self> {
        fn duplicate(fd: i32) -> io::Result<File> {
            // SAFETY: the calling task owns live standard descriptors for this
            // call. try_clone_to_owned creates a separate owned descriptor.
            unsafe { BorrowedFd::borrow_raw(fd) }
                .try_clone_to_owned()
                .map(File::from)
        }
        Ok(Self {
            input: duplicate(0)?,
            output: duplicate(1)?,
            error: duplicate(2)?,
        })
    }

    pub fn line(&mut self) -> io::Result<String> {
        let mut bytes = Vec::with_capacity(128);
        loop {
            let mut byte = [0];
            self.input.read_exact(&mut byte)?;
            if byte[0] == b'\n' {
                return String::from_utf8(bytes).map_err(io::Error::other);
            }
            if bytes.len() == 127 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "local command too long",
                ));
            }
            bytes.push(byte[0]);
        }
    }
}

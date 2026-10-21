//! Synchronous record append and rollback using ordinary Rust I/O.
#![forbid(unsafe_code)]

use crate::record::encode_header;
use crate::{DeviceError, Frame, Storage};
use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};

pub(crate) trait RecordIo: Write + Seek {
    fn truncate(&mut self, length: u64) -> io::Result<()>;
    fn sync(&mut self) -> io::Result<()>;
}

impl RecordIo for File {
    fn truncate(&mut self, length: u64) -> io::Result<()> {
        self.set_len(length)
    }
    fn sync(&mut self) -> io::Result<()> {
        self.sync_all()
    }
}

pub(crate) struct RecordFile<F> {
    file: F,
    max_records: u64,
    max_frame: usize,
    accepted: u64,
    poisoned: bool,
}

impl<F: RecordIo> RecordFile<F> {
    pub(crate) fn new(file: F, max_records: u64, max_frame: usize) -> Result<Self, DeviceError> {
        validate_limits(max_records, max_frame)?;
        Ok(Self {
            file,
            max_records,
            max_frame,
            accepted: 0,
            poisoned: false,
        })
    }
}

pub(crate) fn validate_limits(max_records: u64, max_frame: usize) -> Result<(), DeviceError> {
    if max_records == 0 || max_frame == 0 {
        Err(DeviceError::InvalidData)
    } else {
        Ok(())
    }
}

impl<F: RecordIo> Storage for RecordFile<F> {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError> {
        if self.poisoned {
            return Err(DeviceError::Io);
        }
        if frame.bytes.len() > self.max_frame
            || frame.capture.len != frame.bytes.len()
            || !frame.capture.format.accepts_len(frame.bytes.len())
        {
            return Err(DeviceError::InvalidData);
        }
        if self.accepted == self.max_records {
            return Err(DeviceError::Full);
        }
        let start = self.file.seek(SeekFrom::End(0)).map_err(error)?;
        let header = encode_header(frame.capture, frame.sequence, frame.bytes.len());
        let result = self
            .file
            .write_all(&header)
            .and_then(|()| self.file.write_all(frame.bytes));
        if let Err(failure) = result {
            // A failed append accepts nothing. Unknown rollback state is terminal.
            let restored = self
                .file
                .truncate(start)
                .and_then(|()| self.file.seek(SeekFrom::Start(start)));
            if !matches!(restored, Ok(position) if position == start) {
                self.poisoned = true;
                return Err(DeviceError::Io);
            }
            return Err(error(failure));
        }
        self.accepted += 1;
        Ok(())
    }

    fn flush(&mut self) -> Result<(), DeviceError> {
        if self.poisoned {
            return Err(DeviceError::Io);
        }
        self.file.sync().map_err(error)
    }
}

pub(crate) fn error(error: io::Error) -> DeviceError {
    match error.kind() {
        io::ErrorKind::WouldBlock
        | io::ErrorKind::Interrupted
        | io::ErrorKind::AlreadyExists
        | io::ErrorKind::ResourceBusy => DeviceError::Busy,
        io::ErrorKind::TimedOut => DeviceError::Timeout,
        io::ErrorKind::Unsupported => DeviceError::Unsupported,
        io::ErrorKind::StorageFull | io::ErrorKind::OutOfMemory => DeviceError::Full,
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData => DeviceError::InvalidData,
        _ => DeviceError::Io,
    }
}

#[cfg(test)]
#[path = "file_tests.rs"]
mod tests;

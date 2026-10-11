//! Explicit scripted frame storage for tests, not persistent device support.
#![forbid(unsafe_code)]
use std::collections::VecDeque;
use storage_api::{Capture, DeviceError, Frame, Storage};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredFrame {
    pub sequence: u64,
    pub capture: Capture,
    pub bytes: Vec<u8>,
}

pub struct MockStorage {
    pub records: Vec<StoredFrame>,
    pub capacity: usize,
    pub write_errors: VecDeque<DeviceError>,
    pub flush_failures: usize,
    pub writes: usize,
    pub flushes: usize,
}

impl Default for MockStorage {
    fn default() -> Self {
        Self {
            records: Vec::new(),
            capacity: 512,
            write_errors: VecDeque::new(),
            flush_failures: 0,
            writes: 0,
            flushes: 0,
        }
    }
}

impl Storage for MockStorage {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError> {
        self.writes += 1;
        if let Some(error) = self.write_errors.pop_front() {
            return Err(error);
        }
        if self.records.len() == self.capacity {
            return Err(DeviceError::Full);
        }
        self.records.push(StoredFrame {
            sequence: frame.sequence,
            capture: frame.capture,
            bytes: frame.bytes.to_vec(),
        });
        Ok(())
    }

    fn flush(&mut self) -> Result<(), DeviceError> {
        self.flushes += 1;
        if self.flush_failures > 0 {
            self.flush_failures -= 1;
            return Err(DeviceError::Io);
        }
        Ok(())
    }
}

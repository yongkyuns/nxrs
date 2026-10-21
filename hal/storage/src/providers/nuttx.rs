//! NuttX resource acquisition. Record behavior lives in the shared Rust writer.
#![forbid(unsafe_code)]

use crate::file::{error, validate_limits, RecordFile};
use crate::{DeviceError, Frame, Storage};
use std::fs::{File, OpenOptions};
use std::os::unix::fs::OpenOptionsExt;

/// Synchronous bounded record sink. Append rolls back partial writes; a failed
/// rollback permanently poisons the sink. Flush confirms file synchronization.
pub struct FileStorage {
    records: RecordFile<File>,
}

impl FileStorage {
    pub fn create(path: &str, max_records: u64, max_frame: usize) -> Result<Self, DeviceError> {
        validate_limits(max_records, max_frame)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .map_err(error)?;
        Ok(Self {
            records: RecordFile::new(file, max_records, max_frame)?,
        })
    }
}

impl Storage for FileStorage {
    fn append(&mut self, frame: Frame<'_>) -> Result<(), DeviceError> {
        self.records.append(frame)
    }
    fn flush(&mut self) -> Result<(), DeviceError> {
        self.records.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Capture, Format, PixelFormat};
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[test]
    fn exclusive_private_file_preserves_records_and_rejects_reopen() {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(std::format!(
            "nxrs-record-file-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let mut storage = FileStorage::create(path.to_str().unwrap(), 1, 4).unwrap();
        assert!(matches!(
            FileStorage::create(path.to_str().unwrap(), 1, 4),
            Err(DeviceError::Busy)
        ));
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        storage
            .append(Frame {
                sequence: 1,
                capture: Capture {
                    format: Format {
                        width: 2,
                        height: 2,
                        pixels: PixelFormat::Gray8,
                    },
                    len: 4,
                    timestamp_ms: 10,
                },
                bytes: &[1, 2, 3, 4],
            })
            .unwrap();
        storage.flush().unwrap();
        drop(storage);
        assert_eq!(&std::fs::read(&path).unwrap()[40..], &[1, 2, 3, 4]);
        std::fs::remove_file(path).unwrap();
    }
}

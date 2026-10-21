//! Ordinary-std target probe of the production Rust record writer.
use storage::{Capture, DeviceError, FileStorage, Format, Frame, PixelFormat, Storage};

pub fn run() {
    let path = "/rcam/std-storage.rcam";
    let mut writer = FileStorage::create(path, 2, 4).expect("std storage create");
    assert!(matches!(
        FileStorage::create(path, 2, 4),
        Err(DeviceError::Busy)
    ));
    for sequence in 1..=2 {
        writer
            .append(Frame {
                sequence,
                capture: Capture {
                    format: Format {
                        width: 2,
                        height: 2,
                        pixels: PixelFormat::Gray8,
                    },
                    len: 4,
                    timestamp_ms: sequence * 10,
                },
                bytes: &[1, 2, 3, 4],
            })
            .unwrap();
    }
    writer.flush().expect("std storage sync_all");
    drop(writer);
    let bytes = std::fs::read(path).expect("std storage independent read");
    assert_eq!(bytes.len(), 88);
    for (record, sequence) in bytes.chunks_exact(44).zip(1u64..=2) {
        assert_eq!(&record[..8], b"RCAMREC1");
        assert_eq!(&record[8..12], &[2, 0, 2, 0]);
        assert_eq!(&record[16..24], &sequence.to_le_bytes());
        assert_eq!(&record[24..32], &(sequence * 10).to_le_bytes());
        assert_eq!(&record[32..40], &4u64.to_le_bytes());
        assert_eq!(&record[40..], &[1, 2, 3, 4]);
    }
    // Exercise the same std truncation/seek facilities used by rollback.
    let mut file = std::fs::OpenOptions::new().write(true).open(path).unwrap();
    file.set_len(44).unwrap();
    use std::io::{Seek, SeekFrom};
    assert_eq!(file.seek(SeekFrom::Start(44)).unwrap(), 44);
    file.sync_all().unwrap();
    drop(file);
    assert_eq!(std::fs::metadata(path).unwrap().len(), 44);
    std::fs::remove_file(path).unwrap();
}

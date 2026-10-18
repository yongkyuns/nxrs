#![cfg(nxrs_provider = "mock")]

use storage::{Capture, DeviceError, Format, Frame, MockStorage, PixelFormat, Storage};

#[test]
fn rejection_accepts_nothing_and_flush_failure_is_explicit() {
    let bytes = [7; 4];
    let frame = Frame {
        sequence: 1,
        bytes: &bytes,
        capture: Capture {
            format: Format {
                width: 2,
                height: 2,
                pixels: PixelFormat::Gray8,
            },
            len: 4,
            timestamp_ms: 10,
        },
    };
    let mut storage = MockStorage {
        capacity: 1,
        ..Default::default()
    };
    storage.write_errors.push_back(DeviceError::Busy);
    assert_eq!(storage.append(frame), Err(DeviceError::Busy));
    assert!(storage.records.is_empty());
    assert_eq!(storage.append(frame), Ok(()));
    assert_eq!(storage.records[0].bytes, bytes);
    assert_eq!(storage.records[0].capture, frame.capture);
    assert_eq!(storage.append(frame), Err(DeviceError::Full));
    assert_eq!(storage.records.len(), 1);
    storage.flush_failures = 1;
    assert_eq!(storage.flush(), Err(DeviceError::Io));
    assert_eq!(storage.flush(), Ok(()));
    assert_eq!((storage.writes, storage.flushes), (3, 2));
}

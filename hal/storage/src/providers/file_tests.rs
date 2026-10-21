use super::*;
use crate::{Capture, Format, PixelFormat};
use std::collections::VecDeque;
use std::io::Cursor;
use std::vec::Vec;

enum WriteStep {
    Interrupted,
    Short(usize),
    Zero,
    Fail(io::ErrorKind),
}

#[derive(Default)]
struct Injected {
    bytes: Cursor<Vec<u8>>,
    writes: VecDeque<WriteStep>,
    fail_truncate: bool,
    fail_restore: bool,
    fail_end: bool,
    fail_sync: bool,
    calls: usize,
}

impl Write for Injected {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.calls += 1;
        match self.writes.pop_front() {
            Some(WriteStep::Interrupted) => Err(io::ErrorKind::Interrupted.into()),
            Some(WriteStep::Short(n)) => self.bytes.write(&bytes[..n.min(bytes.len())]),
            Some(WriteStep::Zero) => Ok(0),
            Some(WriteStep::Fail(kind)) => Err(kind.into()),
            None => self.bytes.write(bytes),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Seek for Injected {
    fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
        self.calls += 1;
        if self.fail_end && matches!(position, SeekFrom::End(_)) {
            return Err(io::ErrorKind::Other.into());
        }
        if self.fail_restore && matches!(position, SeekFrom::Start(_)) {
            Err(io::ErrorKind::Other.into())
        } else {
            self.bytes.seek(position)
        }
    }
}

impl RecordIo for Injected {
    fn truncate(&mut self, length: u64) -> io::Result<()> {
        self.calls += 1;
        if self.fail_truncate {
            return Err(io::ErrorKind::Other.into());
        }
        self.bytes.get_mut().truncate(length as usize);
        Ok(())
    }
    fn sync(&mut self) -> io::Result<()> {
        self.calls += 1;
        if self.fail_sync {
            Err(io::ErrorKind::Other.into())
        } else {
            Ok(())
        }
    }
}

fn frame(sequence: u64) -> Frame<'static> {
    Frame {
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
    }
}

#[test]
fn interrupted_and_short_writes_commit_one_complete_record() {
    let mut file = RecordFile::new(Injected::default(), 1, 4).unwrap();
    file.file.writes.extend([
        WriteStep::Interrupted,
        WriteStep::Short(3),
        WriteStep::Short(2),
    ]);
    file.append(frame(7)).unwrap();
    let bytes = file.file.bytes.get_ref();
    assert_eq!(bytes.len(), 44);
    assert_eq!(&bytes[..8], b"RCAMREC1");
    assert_eq!(u64::from_le_bytes(bytes[16..24].try_into().unwrap()), 7);
    assert_eq!(&bytes[40..], &[1, 2, 3, 4]);
    assert_eq!(file.append(frame(8)), Err(DeviceError::Full));
}

#[test]
fn failed_append_restores_bytes_position_and_quota_before_retry() {
    for (steps, expected) in [
        (
            std::vec![WriteStep::Short(5), WriteStep::Zero],
            DeviceError::Io,
        ),
        (
            std::vec![
                WriteStep::Short(5),
                WriteStep::Fail(io::ErrorKind::StorageFull)
            ],
            DeviceError::Full,
        ),
        (
            std::vec![
                WriteStep::Short(40),
                WriteStep::Short(2),
                WriteStep::Fail(io::ErrorKind::Other)
            ],
            DeviceError::Io,
        ),
    ] {
        let mut file = RecordFile::new(Injected::default(), 2, 4).unwrap();
        file.append(frame(1)).unwrap();
        let committed = file.file.bytes.get_ref().clone();
        file.file.writes.extend(steps);
        assert_eq!(file.append(frame(2)), Err(expected));
        assert_eq!(file.file.bytes.get_ref(), &committed);
        assert_eq!(file.file.bytes.position(), 44);
        assert_eq!(file.accepted, 1);
        file.append(frame(2)).unwrap();
        assert_eq!(file.file.bytes.get_ref().len(), 88);
        assert_eq!(file.append(frame(3)), Err(DeviceError::Full));
        file.flush().unwrap();
    }
}

#[test]
fn failing_initial_seek_preserves_records_and_does_not_consume_quota() {
    let mut file = RecordFile::new(Injected::default(), 2, 4).unwrap();
    file.append(frame(1)).unwrap();
    let committed = file.file.bytes.get_ref().clone();
    file.file.fail_end = true;
    assert_eq!(file.append(frame(2)), Err(DeviceError::Io));
    assert_eq!(file.file.bytes.get_ref(), &committed);
    assert_eq!(file.accepted, 1);
    assert!(!file.poisoned);
    file.file.fail_end = false;
    file.append(frame(2)).unwrap();
    assert_eq!(file.file.bytes.get_ref().len(), 88);
}

#[test]
fn failed_rollback_poison_is_sticky_and_performs_no_further_io() {
    for truncate in [true, false] {
        let mut file = RecordFile::new(Injected::default(), 2, 4).unwrap();
        file.append(frame(1)).unwrap();
        file.file.fail_truncate = truncate;
        file.file.fail_restore = !truncate;
        file.file
            .writes
            .extend([WriteStep::Short(5), WriteStep::Fail(io::ErrorKind::Other)]);
        assert_eq!(file.append(frame(2)), Err(DeviceError::Io));
        let calls = file.file.calls;
        for _ in 0..3 {
            assert_eq!(file.append(frame(2)), Err(DeviceError::Io));
            assert_eq!(file.flush(), Err(DeviceError::Io));
        }
        assert_eq!(file.file.calls, calls);
        assert_eq!(file.accepted, 1);
    }
}

#[test]
fn invalid_frames_do_not_touch_the_file_and_sync_failure_remains_retryable() {
    let mut file = RecordFile::new(Injected::default(), 2, 4).unwrap();
    assert_eq!(
        file.append(Frame {
            bytes: &[1],
            ..frame(1)
        }),
        Err(DeviceError::InvalidData)
    );
    assert_eq!(file.file.calls, 0);
    file.append(frame(1)).unwrap();
    file.file.fail_sync = true;
    assert_eq!(file.flush(), Err(DeviceError::Io));
    file.file.fail_sync = false;
    file.flush().unwrap();
    file.append(frame(2)).unwrap();
}

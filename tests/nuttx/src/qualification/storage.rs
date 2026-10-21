//! Target storage failures through the real FileStorage and Recorder path.
//! Only the input frames and selected syscall failures are synthetic.
use nxrs_applications::{AppState, Progress, Recorder};
use storage::{Capture, DeviceError, Format, Frame, PixelFormat};
use nuttx_support::FileStorage;
use nxrs_applications::Error as AppError;
use camera_service::Frames;
use recording_service::{Error, RecordingService, Recordings};

extern "C" {
    fn rc_test_storage_begin(case: u32) -> i32;
    fn rc_test_storage_arm() -> i32;
    fn rc_test_storage_check(phase: u32) -> i32;
    fn rc_test_storage_finish() -> i32;
}

#[derive(Default)]
struct Source {
    sequence: u64,
    bytes: [u8; 4],
}

impl Source {
    fn publish(&mut self, sequence: u8) {
        self.sequence = u64::from(sequence);
        for (index, byte) in self.bytes.iter_mut().enumerate() {
            *byte = sequence + index as u8;
        }
    }
}

impl Frames for Source {
    fn latest_sequence(&self) -> u64 { self.sequence }

    fn next_after(&self, sequence: u64) -> Option<Frame<'_>> {
        (self.sequence > sequence).then_some(Frame {
            sequence: self.sequence,
            capture: Capture {
                format: Format { width: 2, height: 2, pixels: PixelFormat::Gray8 },
                len: self.bytes.len(),
                timestamp_ms: 0x1122_3344_5566_0000 + self.sequence,
            },
            bytes: &self.bytes,
        })
    }
}

pub fn run() {
    // IDs match the fixture-only enum in storage_fault.c. Case 1 completes
    // through EINTR/short writes; 6/7 poison rollback; 8 fails the first fsync.
    for case in 1..=8 {
        // SAFETY: synchronous scalar-only hooks scoped to this target thread;
        // the C fixture retains no Rust references or borrowed buffers.
        assert_eq!(unsafe { rc_test_storage_begin(case) }, 0);
        let storage = FileStorage::create(c"/rcam/storage-fault.rcam", 2, 4).unwrap();
        let mut recordings = RecordingService::new(storage);
        let mut recorder = Recorder::default();
        let mut source = Source::default();
        recorder.start(&source).unwrap();
        source.publish(1);
        assert_eq!(recorder.step(&source, &mut recordings),
                   Ok(Progress::Processed { sequence: 1, skipped: 0 }));
        assert_eq!(unsafe { rc_test_storage_arm() }, 0);
        source.publish(2);
        let first = recorder.step(&source, &mut recordings);
        if case == 1 || case == 8 {
            assert_eq!(first, Ok(Progress::Processed { sequence: 2, skipped: 0 }));
        } else {
            let error = if case == 2 { DeviceError::Full } else { DeviceError::Io };
            assert_eq!(first, Err(AppError::Recording(Error::Device(error))));
            assert_eq!(recorder.stats().processed, 1);
            assert_eq!(recordings.stats().accepted, 1);
        }
        if case == 8 {
            assert_eq!(recorder.stop(&mut recordings), Err(AppError::Recording(Error::Device(DeviceError::Io))));
            assert_eq!(recorder.state(), AppState::Stopping);
            source.publish(3);
            assert_eq!(recorder.step(&source, &mut recordings), Ok(Progress::Idle));
            assert_eq!(recorder.start(&source), Err(AppError::AlreadyRunning));
        }
        assert_eq!(unsafe { rc_test_storage_check(1) }, 0);

        if case == 6 || case == 7 {
            for _ in 0..3 {
                assert_eq!(recorder.step(&source, &mut recordings), Err(AppError::Recording(Error::Device(DeviceError::Io))));
                assert_eq!(recordings.flush(), Err(Error::Device(DeviceError::Io)));
            }
            assert_eq!(recorder.stop(&mut recordings), Err(AppError::Recording(Error::Device(DeviceError::Io))));
            assert_eq!(recorder.state(), AppState::Stopping);
            assert_eq!(recordings.stats().accepted, 1);
            assert_eq!(recorder.stats().processed, 1);
            // C requires syscall counters and actual bytes to remain unchanged.
        } else {
            if case != 1 && case != 8 {
                // The failed append must not advance the reader or consume the
                // second/final storage slot. Retry accepts exactly sequence 2.
                assert_eq!(recorder.step(&source, &mut recordings),
                           Ok(Progress::Processed { sequence: 2, skipped: 0 }));
            }
            assert_eq!(recorder.step(&source, &mut recordings), Ok(Progress::Idle));
            assert_eq!(recordings.stats().accepted, 2);
            assert_eq!(recorder.stats().processed, 2);
            recorder.stop(&mut recordings).unwrap();
            assert_eq!(recorder.state(), AppState::Stopped);
        }
        assert_eq!(recorder.stats().skipped, 0);
        assert_eq!(unsafe { rc_test_storage_check(2) }, 0);
        drop(recordings);
        assert_eq!(unsafe { rc_test_storage_finish() }, 0);
    }
}

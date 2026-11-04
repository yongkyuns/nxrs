//! Shared file recording does not require a configured hardware provider.
use std::io;

#[test]
fn file_recorder_validation_is_accessible_without_platform_selection() {
    let result = storage::recording::storage("not-used", 0, 4, 1);
    assert_eq!(result.err().expect("zero queue is invalid").kind(), io::ErrorKind::InvalidInput);
    let limits = storage::StorageLimits {
        queued_records: 0,
        max_frame_bytes: 4,
        max_records: 1,
    };
    assert_eq!(
        storage::FileRecorder::create_new("not-used", limits)
            .err()
            .expect("invalid capacity must fail before file creation")
            .kind(),
        io::ErrorKind::InvalidInput,
    );
}

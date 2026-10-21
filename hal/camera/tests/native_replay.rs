#![cfg(nxrs_hal_platform = "std")]

use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use camera::{Camera, DeviceError, Format, PixelFormat, ReplayCamera};

const FORMAT: Format = Format {
    width: 2,
    height: 2,
    pixels: PixelFormat::Gray8,
};
static NEXT: AtomicU64 = AtomicU64::new(0);

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        loop {
            let path = std::env::temp_dir().join(format!(
                "nxrs-native-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("temporary directory: {error}"),
            }
        }
    }
    fn join(&self, path: &str) -> PathBuf {
        self.0.join(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn replay_loads_actual_file_and_obeys_clock_capacity_and_restart() {
    let temp = Temp::new();
    let path = temp.join("source.raw");
    fs::write(&path, [7, 7, 7, 7, 9, 9, 9, 9]).unwrap();
    let mut camera = ReplayCamera::load(path, FORMAT, 10, 8).unwrap();
    assert_eq!(camera.frame_count(), 2);
    assert_eq!(
        camera.start(Format { width: 1, ..FORMAT }),
        Err(DeviceError::Unsupported)
    );
    camera.start(FORMAT).unwrap();
    assert_eq!(camera.next_poll_at_ms(), None);
    assert_eq!(camera.poll_frame(100, &mut [0; 3]), Err(DeviceError::Full));
    let mut bytes = [0; 4];
    assert_eq!(
        camera
            .poll_frame(100, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        100
    );
    assert_eq!(bytes, [7; 4]);
    assert_eq!(camera.next_poll_at_ms(), Some(110));
    assert_eq!(camera.poll_frame(109, &mut bytes), Ok(None));
    assert_eq!(camera.next_poll_at_ms(), Some(110));
    assert_eq!(
        camera.poll_frame(108, &mut bytes),
        Err(DeviceError::InvalidData)
    );
    assert_eq!(
        camera
            .poll_frame(110, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        110
    );
    assert_eq!(bytes, [9; 4]);
    assert!(camera.exhausted());
    assert_eq!(camera.next_poll_at_ms(), None);
    assert_eq!(camera.poll_frame(120, &mut bytes), Ok(None));
    camera.stop().unwrap();
    camera.start(FORMAT).unwrap();
    assert_eq!(camera.next_poll_at_ms(), None);
    assert_eq!(
        camera
            .poll_frame(200, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        200
    );
    assert_eq!(camera.next_poll_at_ms(), Some(210));
    assert_eq!(bytes, [7; 4]);
    camera.stop().unwrap();
}

#[test]
fn replay_rejects_empty_truncated_and_oversized_input() {
    let temp = Temp::new();
    let path = temp.join("source.raw");
    for bytes in [vec![], vec![1; 3], vec![1; 12]] {
        fs::write(&path, bytes).unwrap();
        assert!(ReplayCamera::load(&path, FORMAT, 10, 8).is_err());
    }
    fs::write(&path, [1; 4]).unwrap();
    assert!(ReplayCamera::load(&path, FORMAT, 0, 8).is_err());
    assert!(ReplayCamera::load(
        &path,
        Format {
            pixels: PixelFormat::Jpeg,
            ..FORMAT
        },
        10,
        8
    )
    .is_err());
}

#![cfg(hal_mock)]

use camera::{Camera, DeviceError};
use camera::{CameraAction, MockCamera};
const FORMAT: camera::Format = camera::Format {
    width: 2,
    height: 2,
    pixels: camera::PixelFormat::Gray8,
};

#[test]
fn scripted_failures_staging_and_retry_are_unchanged() {
    let mut camera = MockCamera::new([
        CameraAction::Pending,
        CameraAction::Fail(DeviceError::Io),
        CameraAction::Frame(7),
    ]);
    camera.start_failures = 1;
    assert_eq!(camera.start(FORMAT), Err(DeviceError::Io));
    assert!(!camera.active);
    assert_eq!(camera.start(FORMAT), Ok(FORMAT));
    let mut bytes = [0; 4];
    assert_eq!(camera.poll_frame(10, &mut bytes), Ok(None));
    assert_eq!(bytes, [0xee; 4]);
    assert_eq!(camera.poll_frame(20, &mut bytes), Err(DeviceError::Io));
    assert_eq!(
        camera
            .poll_frame(30, &mut bytes)
            .unwrap()
            .unwrap()
            .timestamp_ms,
        30
    );
    assert_eq!(bytes, [7; 4]);
    camera.stop_failures = 1;
    assert_eq!(camera.stop(), Err(DeviceError::Busy));
    assert!(camera.active);
    assert_eq!(camera.stop(), Ok(()));
}

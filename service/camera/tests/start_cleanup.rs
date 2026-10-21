//! A start error must not hide a still-owned device from explicit stop retry.
#![forbid(unsafe_code)]
use camera::{Camera, Capture, DeviceError, Format, PixelFormat};
use camera_service::{CameraService, CameraState, Error};

struct Device {
    stops: usize,
    cleanup_fails: bool,
}

impl Camera for Device {
    fn start(&mut self, _: Format) -> Result<Format, DeviceError> {
        Err(DeviceError::Io)
    }
    fn poll_frame(&mut self, _: u64, _: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        panic!("a failed start must not admit capture")
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.stops += 1;
        if self.cleanup_fails && self.stops == 1 {
            Err(DeviceError::Busy)
        } else {
            Ok(())
        }
    }
}

#[test]
fn failed_start_keeps_failed_cleanup_retryable() {
    let format = Format {
        width: 2,
        height: 2,
        pixels: PixelFormat::Gray8,
    };
    for cleanup_fails in [false, true] {
        let mut service = CameraService::<_, 4, 1>::new(Device {
            stops: 0,
            cleanup_fails,
        })
        .unwrap();
        assert_eq!(service.start(format), Err(Error::Device(DeviceError::Io)));
        assert_eq!(service.backend().stops, 1);
        if cleanup_fails {
            assert_eq!(service.state(), CameraState::StopPending);
            assert_eq!(service.start(format), Err(Error::AlreadyRunning));
        } else {
            assert_eq!(service.state(), CameraState::Stopped);
        }
        service.stop().unwrap();
        assert_eq!(service.state(), CameraState::Stopped);
        assert_eq!(service.backend().stops, if cleanup_fails { 2 } else { 1 });
    }
}

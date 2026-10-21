//! Shared full-integration behavior for the temporary core-only fixture and
//! the ordinary-std NuttX application. Platform runtime mechanics stay outside.
use core::ffi::CStr;

use nxrs_applications::{AppState, CameraProduct, Progress};
use camera::{Camera, DeviceError, Format, PixelFormat};
use nuttx_support::DeviceCamera;
use nuttx_support::FileStorage;
use transport::Transport;
use camera_service::{CameraState, CaptureProgress};

extern "C" {
    fn rc_sim_note(phase: u32);
}

fn note(phase: u32) {
    // SAFETY: scalar-only target diagnostic hook, no retained pointers.
    unsafe { rc_sim_note(phase) };
}

pub fn run<T, N, S>(
    output: &CStr,
    transport: T,
    now_ms: N,
    sleep_ms: S,
) -> i32
where
    T: Transport,
    N: Fn() -> u64,
    S: Fn(u64),
{
    crate::qualification::run();

    let format = Format { width: 2, height: 2, pixels: PixelFormat::Gray8 };
    let path = c"/dev/nxrs-frame";
    let mut camera = DeviceCamera::new(path);
    assert_eq!(
        camera.start(Format { width: 1, ..format }),
        Err(DeviceError::Unsupported)
    );
    // An unsupported request must have closed and joined its driver instance.
    note(1);

    let before = now_ms();
    sleep_ms(20);
    assert!(now_ms() > before);
    note(2);

    let storage = FileStorage::create(output, 3, 4).expect("NuttX file create");
    assert!(matches!(
        FileStorage::create(output, 3, 4),
        Err(storage::DeviceError::Busy)
    ));
    let mut product = CameraProduct::<_, _, _, 4, 2>::new(camera, storage, transport).unwrap();
    assert_eq!(product.camera.start(format), Ok(format));
    product.recorder.start(&product.camera).unwrap();
    product.monitor.start(&product.camera).unwrap();
    let deadline = now_ms() + 5000;

    for sequence in 1..=4u64 {
        loop {
            let now = now_ms();
            assert!(now < deadline, "target capture deadline");
            let report = product.step(now);
            assert!(report.recorder.is_ok(), "target recording failed");
            assert!(report.monitor.is_ok(), "target telemetry failed");
            match report.camera.unwrap() {
                CaptureProgress::Pending => sleep_ms(1),
                CaptureProgress::Published(number) => {
                    assert_eq!(number, sequence);
                    assert_eq!(
                        report.monitor,
                        Ok(Progress::Processed { sequence, skipped: 0 })
                    );
                    if sequence == 2 {
                        assert_eq!(report.recorder, Ok(Progress::Idle));
                    } else {
                        assert_eq!(
                            report.recorder,
                            Ok(Progress::Processed { sequence, skipped: 0 })
                        );
                    }
                    break;
                }
            }
        }
        if sequence == 1 {
            product.recorder.stop(&mut product.recordings).unwrap();
            assert_eq!(product.camera.state(), CameraState::Running);
        }
        if sequence == 2 {
            assert_eq!(product.monitor.stats().processed, 2);
            product.recorder.start(&product.camera).unwrap();
        }
    }

    assert_eq!(product.recorder.stats().processed, 3);
    assert_eq!(product.monitor.stats().processed, 4);
    assert_eq!(product.recorder.stats().skipped, 0);
    assert_eq!(product.monitor.stats().skipped, 0);
    assert_eq!(product.camera.poll(now_ms()), Ok(CaptureProgress::Pending));
    note(3);

    let shutdown = product.shutdown();
    assert_eq!(shutdown.recorder, Ok(()));
    assert_eq!(shutdown.camera, Ok(()));
    assert_eq!(product.recorder.state(), AppState::Stopped);
    assert_eq!(product.monitor.state(), AppState::Stopped);
    assert_eq!(product.camera.state(), CameraState::Stopped);
    drop(product);
    note(4);
    0
}

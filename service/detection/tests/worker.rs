use camera::{Capture, Format, Frame, PixelFormat};
use detection_service::{Error, Offer, Score, Worker, WorkerOptions};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::time::{Duration, Instant};
fn options() -> WorkerOptions {
    WorkerOptions {
        frame_capacity: 32,
        sample_interval_ms: 10,
        stack_bytes: 65536,
    }
}
fn frame(sequence: u64, bytes: &[u8]) -> Frame<'_> {
    Frame {
        sequence,
        capture: Capture {
            format: Format {
                width: 96,
                height: 96,
                pixels: PixelFormat::Jpeg,
            },
            len: bytes.len(),
            timestamp_ms: sequence,
        },
        bytes,
    }
}
struct Blocked {
    entered: SyncSender<Vec<u8>>,
    resume: Receiver<()>,
}
impl detector::Detector for Blocked {
    fn capabilities(&self) -> detector::Capabilities {
        detector::Capabilities::localized(96, 96)
    }
    fn detect(
        &mut self,
        image: detector::Image<'_>,
    ) -> Result<detector::DetectionResult, detector::DeviceError> {
        let bytes = image.bytes;
        self.entered
            .try_send(bytes.to_vec())
            .map_err(|_| detector::DeviceError::Model)?;
        self.resume
            .recv_timeout(Duration::from_secs(10))
            .map_err(|_| detector::DeviceError::Model)?;
        let score = Score::new(900)?;
        let mut objects = detector::Detections::default();
        objects.boxes[0] = Some(detector::Detection {
            score,
            bounds: detector::Bounds {
                left: 100,
                top: 200,
                right: 300,
                bottom: 400,
            },
        });
        Ok(detector::DetectionResult {
            person_score: score,
            objects,
        })
    }
}
#[test]
fn overload_never_blocks_or_borrows_camera_and_returned_storage_is_reused() {
    let (entered, entered_rx) = sync_channel(1);
    let (resume, resume_rx) = sync_channel(1);
    let mut worker = Worker::spawn(options(), move || {
        Ok(Blocked {
            entered,
            resume: resume_rx,
        })
    })
    .unwrap();
    let mut source = [1, 2, 3, 4];
    assert_eq!(worker.offer(frame(1, &source), 10), Ok(Offer::Accepted));
    source.fill(99);
    assert_eq!(
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap(),
        [1, 2, 3, 4]
    );
    assert_eq!(worker.offer(frame(2, &source), 20), Ok(Offer::Busy));
    resume.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(done) = worker.poll().unwrap() {
            assert_eq!(done.sequence, 1);
            assert_eq!(done.objects.len(), 1);
            assert_eq!(done.objects.iter().next().unwrap().bounds.left, 100);
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(worker.offer(frame(3, &source), 21), Ok(Offer::Accepted));
    assert_eq!(
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap(),
        [99; 4]
    );
    resume.send(()).unwrap();
    worker.stop().unwrap();
    assert_eq!(worker.poll(), Err(Error::Stopped));
    assert_eq!(worker.offer(frame(4, &source), 40), Err(Error::Stopped));
    worker.stop().unwrap();
}

struct Panics;
impl detector::Detector for Panics {
    fn capabilities(&self) -> detector::Capabilities {
        detector::Capabilities::presence(96, 96)
    }
    fn detect(
        &mut self,
        _: detector::Image<'_>,
    ) -> Result<detector::DetectionResult, detector::DeviceError> {
        panic!("injected worker failure")
    }
}
#[test]
fn worker_panic_is_sticky_and_cannot_be_reported_as_clean_shutdown() {
    let mut worker = Worker::spawn(options(), || Ok(Panics)).unwrap();
    worker.offer(frame(1, &[1]), 10).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match worker.poll() {
            Err(Error::Worker) => break,
            Ok(None) => {}
            other => panic!("unexpected worker result {other:?}"),
        }
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert_eq!(worker.offer(frame(2, &[1]), 20), Err(Error::Worker));
    assert_eq!(worker.stop(), Err(Error::Worker));
    assert_eq!(worker.stop(), Err(Error::Worker));
}
#[test]
fn failed_initializer_is_joined_and_invalid_capacity_does_not_initialize() {
    assert!(matches!(
        Worker::spawn::<Blocked>(options(), || Err(Error::Model)),
        Err(Error::Model)
    ));
    assert!(matches!(
        Worker::spawn::<Blocked>(
            WorkerOptions {
                frame_capacity: 0,
                ..options()
            },
            || panic!("must not run")
        ),
        Err(Error::Configuration)
    ));
}

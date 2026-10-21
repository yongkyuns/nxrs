use camera::{Capture, Format, Frame, PixelFormat};
use detection_service::{Error, Offer, PersonDetector, Score, Worker, WorkerOptions};
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
impl PersonDetector for Blocked {
    fn detect(&mut self, bytes: &[u8], _: u16, _: u16) -> Result<Score, Error> {
        self.entered
            .try_send(bytes.to_vec())
            .map_err(|_| Error::Model)?;
        self.resume
            .recv_timeout(Duration::from_secs(10))
            .map_err(|_| Error::Model)?;
        Score::new(900)
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
impl PersonDetector for Panics {
    fn detect(&mut self, _: &[u8], _: u16, _: u16) -> Result<Score, Error> {
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

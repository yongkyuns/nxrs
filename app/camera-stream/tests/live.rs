use camera::{Camera, Capture, DeviceError, Format, PixelFormat};
use camera_stream::LiveCamera;
use std::cell::Cell;
use std::net::TcpListener;
use std::rc::Rc;
use stream_service::Options;

struct Source {
    format: Option<Format>,
    stops: Rc<Cell<usize>>,
    negotiated_pixels: Option<PixelFormat>,
}
impl Camera for Source {
    fn start(&mut self, mut format: Format) -> Result<Format, DeviceError> {
        if let Some(pixels) = self.negotiated_pixels {
            format.pixels = pixels;
        }
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, bytes: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        bytes[..4].copy_from_slice(&[0xff, 0xd8, 0xff, 0xd9]);
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: 4,
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.stops.set(self.stops.get() + 1);
        self.format = None;
        Ok(())
    }
    fn discarded_frames(&self) -> Option<u64> {
        Some(3)
    }
}

#[test]
fn capture_continues_offline_and_without_a_viewer() {
    let stops = Rc::new(Cell::new(0));
    let source = Source {
        format: None,
        stops: stops.clone(),
        negotiated_pixels: None,
    };
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let mut app = LiveCamera::<_, 64, _>::new(
        source,
        listener,
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    app.start(320, 240).unwrap();
    for now in 0..100 {
        let progress = app.poll(now, now >= 50).unwrap();
        assert!(!progress.active && !progress.frame_accepted);
    }
    let stats = app.stats();
    assert_eq!(stats.captured, 100);
    assert_eq!(stats.captured_bytes, 400);
    assert_eq!(stats.max_frame_bytes, 4);
    assert_eq!(stats.camera_discarded, Some(3));
    assert_eq!(stats.stream.accepted, 0);
    app.stop().unwrap();
    assert_eq!(stops.get(), 1);
    assert!(app.poll(100, true).is_err());
}

#[test]
fn non_jpeg_negotiation_stops_before_streaming() {
    let stops = Rc::new(Cell::new(0));
    let source = Source {
        format: None,
        stops: stops.clone(),
        negotiated_pixels: Some(PixelFormat::Gray8),
    };
    let mut app = LiveCamera::<_, 64, _>::new(
        source,
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    assert!(matches!(
        app.start(2, 2),
        Err(camera_stream::Error::Camera(
            camera_service::Error::InvalidFormat
        ))
    ));
    assert_eq!(stops.get(), 1);
    assert!(app.poll(0, true).is_err());
    app.stop().unwrap();
    assert_eq!(stops.get(), 1);
}

#[test]
fn blocked_inference_never_holds_capture_and_stop_joins_before_restart() {
    use detection_service::{Error, Policy, Score, Worker, WorkerOptions};
    use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
    use std::time::Duration;
    struct Detector {
        entered: SyncSender<()>,
        resume: Receiver<()>,
    }
    impl detector::Detector for Detector {
        fn capabilities(&self) -> detector::Capabilities {
            detector::Capabilities::presence(96, 96)
        }
        fn detect(
            &mut self,
            _: detector::Image<'_>,
        ) -> Result<detector::DetectionResult, detector::DeviceError> {
            let _ = self.entered.try_send(());
            self.resume
                .recv_timeout(Duration::from_secs(10))
                .map_err(|_| detector::DeviceError::Model)?;
            Ok(detector::DetectionResult::presence(Score::new(900)?))
        }
    }
    let (entered, entered_rx) = sync_channel(1);
    let (resume, resume_rx) = sync_channel(1);
    let worker = Worker::spawn(
        WorkerOptions {
            frame_capacity: 64,
            sample_interval_ms: 1,
            stack_bytes: 65536,
        },
        move || {
            Ok(Detector {
                entered,
                resume: resume_rx,
            })
        },
    )
    .unwrap();
    let stops = Rc::new(Cell::new(0));
    let mut app = LiveCamera::<_, 64, _>::new(
        Source {
            format: None,
            stops: stops.clone(),
            negotiated_pixels: None,
        },
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    app.attach_detection(camera_stream::SecurityDetection::new(worker, Policy::default()).unwrap())
        .unwrap();
    app.start(320, 240).unwrap();
    app.poll(0, false).unwrap();
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    // This checks ownership/progress under an explicitly blocked model. It is
    // a synthetic source and makes no physical FPS claim.
    for now in 1..1000 {
        app.poll(now, false).unwrap();
    }
    assert_eq!(app.stats().captured, 1000);
    assert_eq!(app.detection().unwrap().snapshot().worker.submitted, 1);
    resume.send(()).unwrap();
    app.stop().unwrap();
    assert_eq!(stops.get(), 1);
    assert!(matches!(
        app.start(320, 240),
        Err(camera_stream::Error::Detection(Error::Stopped))
    ));
}

#[test]
fn camera_stop_failure_still_joins_and_releases_the_detector() {
    use detection_service::{Policy, Score, Worker, WorkerOptions};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    struct FailedStop(Source);
    impl Camera for FailedStop {
        fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
            self.0.start(format)
        }
        fn poll_frame(
            &mut self,
            now: u64,
            bytes: &mut [u8],
        ) -> Result<Option<Capture>, DeviceError> {
            self.0.poll_frame(now, bytes)
        }
        fn stop(&mut self) -> Result<(), DeviceError> {
            Err(DeviceError::Io)
        }
    }
    struct Detector(Arc<AtomicBool>);
    impl detector::Detector for Detector {
        fn capabilities(&self) -> detector::Capabilities {
            detector::Capabilities::presence(96, 96)
        }
        fn detect(
            &mut self,
            _: detector::Image<'_>,
        ) -> Result<detector::DetectionResult, detector::DeviceError> {
            Ok(detector::DetectionResult::presence(Score::new(900)?))
        }
    }
    impl Drop for Detector {
        fn drop(&mut self) {
            self.0.store(true, Ordering::Release);
        }
    }
    let released = Arc::new(AtomicBool::new(false));
    let witness = released.clone();
    let worker = Worker::spawn(
        WorkerOptions {
            frame_capacity: 64,
            sample_interval_ms: 1,
            stack_bytes: 65536,
        },
        move || Ok(Detector(witness)),
    )
    .unwrap();
    let mut app = LiveCamera::<_, 64, _>::new(
        FailedStop(Source {
            format: None,
            stops: Rc::new(Cell::new(0)),
            negotiated_pixels: None,
        }),
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [0; 64],
        [0; 64],
        [0; 64],
        Options::default(),
    )
    .unwrap();
    app.attach_detection(camera_stream::SecurityDetection::new(worker, Policy::default()).unwrap())
        .unwrap();
    app.start(320, 240).unwrap();
    app.poll(0, false).unwrap();
    assert!(matches!(app.stop(), Err(camera_stream::Error::Camera(_))));
    assert!(released.load(Ordering::Acquire));
    assert_eq!(
        app.detection().unwrap().snapshot().presence,
        detection_service::Presence::Unavailable
    );
}

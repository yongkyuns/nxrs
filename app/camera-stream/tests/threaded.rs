use camera::{Camera, Capture, DeviceError, Format};
use camera_stream::{Error, SecurityDetection, ThreadedCamera};
use detection_service::{Policy, Presence, Score, Worker};
use std::cell::Cell;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::rc::Rc;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc, Arc,
};
use std::time::{Duration, Instant};
use stream_service::{FramePublish, Options, Server, StreamWorker, WorkerOptions};

struct Source {
    format: Option<Format>,
    stops: Rc<Cell<usize>>,
    fail_stop: bool,
}
impl Camera for Source {
    fn start(&mut self, format: Format) -> Result<Format, DeviceError> {
        self.format = Some(format);
        Ok(format)
    }
    fn poll_frame(&mut self, now: u64, out: &mut [u8]) -> Result<Option<Capture>, DeviceError> {
        out[..4].copy_from_slice(&[0xff, 0xd8, 0xff, 0xd9]);
        Ok(Some(Capture {
            format: self.format.ok_or(DeviceError::Io)?,
            len: 4,
            timestamp_ms: now,
        }))
    }
    fn stop(&mut self) -> Result<(), DeviceError> {
        self.stops.set(self.stops.get() + 1);
        if self.fail_stop {
            Err(DeviceError::Io)
        } else {
            self.format = None;
            Ok(())
        }
    }
}
struct Detector {
    entered: Option<mpsc::SyncSender<()>>,
    resume: Option<mpsc::Receiver<()>>,
    retired: Arc<AtomicBool>,
}
impl detector::Detector for Detector {
    fn capabilities(&self) -> detector::Capabilities {
        detector::Capabilities::presence(96, 96)
    }
    fn detect(
        &mut self,
        _: detector::Image<'_>,
    ) -> Result<detector::DetectionResult, detector::DeviceError> {
        if let Some(entered) = self.entered.take() {
            entered.send(()).unwrap();
        }
        if let Some(resume) = self.resume.take() {
            resume.recv_timeout(Duration::from_secs(5)).unwrap();
        }
        Ok(detector::DetectionResult::presence(Score::new(900)?))
    }
}
impl Drop for Detector {
    fn drop(&mut self) {
        self.retired.store(true, Ordering::Release);
    }
}
fn detection(
    entered: Option<mpsc::SyncSender<()>>,
    resume: Option<mpsc::Receiver<()>>,
    retired: Arc<AtomicBool>,
) -> SecurityDetection {
    let worker = Worker::spawn(
        detection_service::WorkerOptions {
            frame_capacity: 64,
            sample_interval_ms: 1,
            stack_bytes: 65536,
        },
        move || {
            Ok(Detector {
                entered,
                resume,
                retired,
            })
        },
    )
    .unwrap();
    SecurityDetection::new(worker, Policy::default()).unwrap()
}
fn options() -> WorkerOptions {
    WorkerOptions {
        stack_bytes: 256 * 1024,
        poll_interval: Duration::from_millis(1),
    }
}
fn source(stops: Rc<Cell<usize>>, fail_stop: bool) -> Source {
    Source {
        format: None,
        stops,
        fail_stop,
    }
}
fn idle_worker(capacity: usize) -> StreamWorker<Vec<u8>> {
    let run = |_: stream_service::WorkerReceiver<Vec<u8>>, stop: stream_service::WorkerStop| {
        while !stop.is_requested() {
            std::thread::park_timeout(Duration::from_millis(1));
        }
        Ok(())
    };
    if capacity == 0 {
        StreamWorker::spawn(vec![0; 64], options(), run).unwrap()
    } else {
        StreamWorker::spawn_with_metadata(
            vec![0; 64],
            vec![0; capacity].into_boxed_slice(),
            options(),
            run,
        )
        .unwrap()
    }
}
#[test]
fn detection_requires_full_publication_storage_and_retirement_on_failed_camera_stop() {
    for capacity in [
        0,
        detection_wire::MAX_SOURCE_BYTES - 1,
        detection_wire::MAX_SOURCE_BYTES,
    ] {
        let retired = Arc::new(AtomicBool::new(false));
        let stops = Rc::new(Cell::new(0));
        let mut app = ThreadedCamera::<_, 64, _, _>::new(
            source(stops.clone(), true),
            [0; 64],
            [0; 64],
            idle_worker(capacity),
        )
        .unwrap();
        let result = app.attach_detection(detection(None, None, retired.clone()));
        if capacity < detection_wire::MAX_SOURCE_BYTES {
            assert!(matches!(
                result,
                Err(Error::Detection(detection_service::Error::Configuration))
            ));
            assert!(retired.load(Ordering::Acquire));
        } else {
            result.unwrap();
            app.start(320, 240).unwrap();
            assert!(matches!(app.stop(), Err(Error::Camera(_))));
            assert!(retired.load(Ordering::Acquire));
            assert_eq!(stops.get(), 1);
            assert!(matches!(
                app.start(320, 240),
                Err(Error::Detection(detection_service::Error::Stopped))
            ));
        }
        app.finish_worker().unwrap();
    }
}
struct HeldIo {
    socket: TcpStream,
    entered: Option<mpsc::SyncSender<()>>,
    producer_entered: Option<mpsc::SyncSender<()>>,
    resume: mpsc::Receiver<()>,
    retired: Arc<AtomicBool>,
}
impl Read for HeldIo {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        if let Some(entered) = self.entered.take() {
            entered.send(()).unwrap();
            if let Some(producer) = self.producer_entered.take() {
                producer.send(()).unwrap();
            }
            self.resume.recv_timeout(Duration::from_secs(5)).unwrap();
        }
        self.socket.read(out)
    }
}
impl Write for HeldIo {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.socket.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.socket.flush()
    }
}
impl Drop for HeldIo {
    fn drop(&mut self) {
        self.retired.store(true, Ordering::Release);
    }
}
// Force the receiver's try-lock fallback while the producer owns the exchange.
// This is test storage, not a hook in the production handoff or network API.
struct GatedBuffer {
    bytes: Vec<u8>,
    armed: Arc<AtomicBool>,
    admit: mpsc::SyncSender<()>,
    network_entered: mpsc::Receiver<()>,
}
impl AsRef<[u8]> for GatedBuffer {
    fn as_ref(&self) -> &[u8] {
        &self.bytes
    }
}
impl AsMut<[u8]> for GatedBuffer {
    fn as_mut(&mut self) -> &mut [u8] {
        if self.armed.swap(false, Ordering::AcqRel) {
            self.admit.send(()).unwrap();
            self.network_entered
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
        &mut self.bytes
    }
}
#[test]
fn blocked_inference_and_network_keep_capture_nonblocking_and_retire_in_order() {
    blocked_owners(false);
}
#[test]
fn network_poll_outside_frame_lock_allows_capture_publication_and_joined_retirement() {
    blocked_owners(true);
}
fn blocked_owners(outside_frame_lock: bool) {
    let (port_send, port) = mpsc::sync_channel(1);
    let (io_enter_send, io_enter) = mpsc::sync_channel(1);
    let (producer_enter_send, producer_enter) = mpsc::sync_channel(1);
    let (io_resume_send, io_resume) = mpsc::sync_channel(1);
    let (admit_send, admit) = mpsc::sync_channel(1);
    let armed = Arc::new(AtomicBool::new(false));
    let buffer = GatedBuffer {
        bytes: vec![0; 64],
        armed: armed.clone(),
        admit: admit_send.clone(),
        network_entered: producer_enter,
    };
    let network_retired = Arc::new(AtomicBool::new(false));
    let network_witness = network_retired.clone();
    let worker = StreamWorker::spawn_with_metadata(
        buffer,
        vec![0; detection_wire::MAX_SOURCE_BYTES].into_boxed_slice(),
        options(),
        move |mut frames, stop| {
            let listener = TcpListener::bind("127.0.0.1:0")?;
            let port = listener.local_addr()?.port();
            let mut gate = Some((io_enter_send, producer_enter_send, io_resume));
            let mut server =
                Server::with_transport(listener, vec![0; 64], Options::default(), move |socket| {
                    let (entered, producer_entered, resume) = gate.take().unwrap();
                    Ok(HeldIo {
                        socket,
                        entered: Some(entered),
                        producer_entered: Some(producer_entered),
                        resume,
                        retired: network_witness.clone(),
                    })
                })?;
            port_send.send(port).unwrap();
            let mut admission = Some(admit);
            frames.serve_with_control(
                &mut server,
                &stop,
                || 0,
                || {
                    if let Some(admission) = admission.take() {
                        admission.recv_timeout(Duration::from_secs(5)).unwrap();
                    }
                    Ok(stream_service::WorkerAction::Continue)
                },
            )
        },
    )
    .unwrap();
    let port = port.recv_timeout(Duration::from_secs(2)).unwrap();
    let stops = Rc::new(Cell::new(0));
    let retired = Arc::new(AtomicBool::new(false));
    let (enter_send, enter) = mpsc::sync_channel(1);
    let (resume_send, resume) = mpsc::sync_channel(1);
    let mut app =
        ThreadedCamera::<_, 64, _, _>::new(source(stops.clone(), false), [0; 64], [0; 64], worker)
            .unwrap();
    app.attach_detection(detection(Some(enter_send), Some(resume), retired.clone()))
        .unwrap();
    app.start(320, 240).unwrap();
    assert_eq!(app.poll(0, false).unwrap(), Some(FramePublish::Published));
    enter.recv_timeout(Duration::from_secs(2)).unwrap();
    let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
    peer.write_all(b"GET /stream?metadata=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    // In the first case, admission waits until the producer has released the
    // slot. In the second, AsMut admits the receiver while that slot is held,
    // forcing its network-only poll without the frame lock.
    armed.store(outside_frame_lock, Ordering::Release);
    assert_eq!(app.poll(1, true).unwrap(), Some(FramePublish::Published));
    if !outside_frame_lock {
        admit_send.send(()).unwrap();
    }
    io_enter.recv_timeout(Duration::from_secs(2)).unwrap();
    let expected = if outside_frame_lock {
        FramePublish::Published
    } else {
        FramePublish::Busy
    };
    let started = Instant::now();
    for now in 2..1002 {
        assert_eq!(app.poll(now, true).unwrap(), Some(expected));
    }
    assert!(started.elapsed() < Duration::from_millis(300));
    let stats = app.stats();
    assert_eq!(stats.capture.captured, 1002);
    assert_eq!(stats.handed_off, if outside_frame_lock { 1002 } else { 2 });
    assert_eq!(
        stats.skipped_busy,
        if outside_frame_lock { 0 } else { 1000 }
    );
    assert_eq!(stats.rejected, 0);
    assert_eq!(stats.network_closed, 0);
    assert_eq!(app.detection().unwrap().snapshot().worker.submitted, 1);
    assert!(!retired.load(Ordering::Acquire));
    resume_send.send(()).unwrap();
    app.stop().unwrap();
    assert!(retired.load(Ordering::Acquire));
    assert_eq!(stops.get(), 1);
    assert!(!network_retired.load(Ordering::Acquire));
    io_resume_send.send(()).unwrap();
    app.finish_worker().unwrap();
    assert!(network_retired.load(Ordering::Acquire));
}
#[test]
fn threaded_stream_serializes_confirmed_and_expired_detection_without_waiting_for_a_model() {
    for stale in [false, true] {
        let (port_send, port) = mpsc::sync_channel(1);
        let (release_send, release) = mpsc::sync_channel(1);
        let worker = StreamWorker::spawn_with_metadata(
            vec![0; 64],
            vec![0; detection_wire::MAX_SOURCE_BYTES].into_boxed_slice(),
            options(),
            move |mut frames, stop| {
                let listener = TcpListener::bind("127.0.0.1:0")?;
                port_send.send(listener.local_addr()?.port()).unwrap();
                let mut server = Server::new(
                    listener,
                    vec![0; 64],
                    Options {
                        max_frame_age_ms: None,
                        ..Options::default()
                    },
                )?;
                release.recv_timeout(Duration::from_secs(5)).unwrap();
                frames.serve(&mut server, &stop, || 100000)
            },
        )
        .unwrap();
        let port = port.recv_timeout(Duration::from_secs(2)).unwrap();
        let retired = Arc::new(AtomicBool::new(false));
        let mut app = ThreadedCamera::<_, 64, _, _>::new(
            source(Rc::new(Cell::new(0)), false),
            [0; 64],
            [0; 64],
            worker,
        )
        .unwrap();
        app.attach_detection(detection(None, None, retired.clone()))
            .unwrap();
        app.start(320, 240).unwrap();
        let mut now = 0;
        let deadline = Instant::now() + Duration::from_secs(2);
        while app.detection().unwrap().snapshot().presence != Presence::Person {
            app.poll(now, false).unwrap();
            now += 1;
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(1));
        }
        if stale {
            now += 6000;
        }
        let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        peer.write_all(b"GET /stream?metadata=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .unwrap();
        assert_eq!(app.poll(now, true).unwrap(), Some(FramePublish::Published));
        release_send.send(()).unwrap();
        let mut bytes = Vec::new();
        let mut buffer = [0; 8192];
        while !bytes.ends_with(&[0xff, 0xd9, b'\r', b'\n']) {
            let n = peer.read(&mut buffer).unwrap();
            assert!(n > 0);
            bytes.extend_from_slice(&buffer[..n]);
            assert!(bytes.len() < 8192);
        }
        let text = std::str::from_utf8(&bytes[..bytes.len() - 6]).unwrap();
        let kind = "Content-Type: application/vnd.nxrs.detection+json\r\n";
        let offset = text.find(kind).unwrap() + kind.len();
        let (headers, rest) = text[offset..].split_once("\r\n\r\n").unwrap();
        let length = headers
            .strip_prefix("Content-Length: ")
            .unwrap()
            .parse::<usize>()
            .unwrap();
        let publication = detection_wire::Source::decode(&rest.as_bytes()[..length]).unwrap();
        assert_eq!(
            publication.presence,
            if stale {
                detection_wire::Presence::Unavailable
            } else {
                detection_wire::Presence::Person
            }
        );
        if stale {
            assert!(publication.latest.is_none());
        } else {
            assert!(publication.latest.unwrap().frame_sequence <= app.stats().capture.captured);
        }
        app.stop().unwrap();
        app.finish_worker().unwrap();
        assert!(retired.load(Ordering::Acquire));
    }
}

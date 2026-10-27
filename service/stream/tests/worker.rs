use camera::{Capture, Format, Frame, PixelFormat};
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};
use stream_service::{FramePublish, Options, Server, StreamWorker, WorkerAction, WorkerOptions};
const JPEG: &[u8] = &[0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9];
fn frame(sequence: u64) -> Frame<'static> {
    Frame {
        sequence,
        bytes: JPEG,
        capture: Capture {
            format: Format {
                width: 320,
                height: 240,
                pixels: PixelFormat::Jpeg,
            },
            len: JPEG.len(),
            timestamp_ms: 0,
        },
    }
}
fn options() -> WorkerOptions {
    WorkerOptions {
        stack_bytes: 256 * 1024,
        poll_interval: Duration::from_millis(2),
    }
}
fn until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(3);
    while !predicate() {
        assert!(Instant::now() < deadline, "worker control deadline");
        std::thread::sleep(Duration::from_millis(1));
    }
}
struct HeldSocket {
    socket: TcpStream,
    entered: Option<mpsc::SyncSender<()>>,
    release: mpsc::Receiver<()>,
    retired: Arc<AtomicBool>,
}
impl Read for HeldSocket {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if let Some(entered) = self.entered.take() {
            entered.send(()).unwrap();
            self.release.recv_timeout(Duration::from_secs(2)).unwrap();
        }
        self.socket.read(bytes)
    }
}
impl Write for HeldSocket {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.socket.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.socket.flush()
    }
}
impl Drop for HeldSocket {
    fn drop(&mut self) {
        self.retired.store(true, Ordering::Release);
    }
}
#[test]
fn capture_handoff_never_waits_for_held_network_io_and_finish_observes_retirement() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (entered_send, entered) = mpsc::sync_channel(1);
    let (release_send, release) = mpsc::sync_channel(1);
    let retired = Arc::new(AtomicBool::new(false));
    let retired_worker = retired.clone();
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |mut frames, stop| {
        let epoch = Instant::now();
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        let mut gate = Some((entered_send, release));
        let mut server =
            Server::with_transport(listener, vec![0; 64], Options::default(), move |socket| {
                let (entered, release) = gate.take().expect("one admitted test peer");
                Ok(HeldSocket {
                    socket,
                    entered: Some(entered),
                    release,
                    retired: retired_worker.clone(),
                })
            })?;
        ready_send.send(port).unwrap();
        frames.serve(&mut server, &stop, || epoch.elapsed().as_millis() as u64)
    })
    .unwrap();
    let port = ready.recv_timeout(Duration::from_secs(2)).unwrap();
    // Readiness precedes the owner's first statistics update. That bounded
    // try-lock may race this setup handoff even while admission is offline.
    until(|| match worker.publish(frame(1)) {
        FramePublish::Published => true,
        FramePublish::Busy => false,
        other => panic!("initial frame handoff refused: {other:?}"),
    });
    let peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
    // It starts offline, so no connection reaches its transport/crypto factory.
    assert!(entered.recv_timeout(Duration::from_millis(20)).is_err());
    worker.set_online(true);
    entered.recv_timeout(Duration::from_secs(1)).unwrap();
    let started = Instant::now();
    for sequence in 2..1002 {
        assert_eq!(worker.publish(frame(sequence)), FramePublish::Busy);
    }
    assert!(started.elapsed() < Duration::from_millis(200));
    assert!(worker.stats().is_none());
    assert!(!retired.load(Ordering::Acquire));
    worker.request_stop();
    assert_eq!(worker.publish(frame(1002)), FramePublish::Closed);
    let (finished_send, finished) = mpsc::sync_channel(1);
    let joiner = std::thread::spawn(move || finished_send.send(worker.finish()).unwrap());
    assert!(finished.recv_timeout(Duration::from_millis(20)).is_err());
    assert!(!retired.load(Ordering::Acquire));
    release_send.send(()).unwrap();
    finished
        .recv_timeout(Duration::from_secs(2))
        .unwrap()
        .unwrap();
    assert!(retired.load(Ordering::Acquire));
    joiner.join().unwrap();
    drop(peer);
}
#[test]
fn worker_failure_stays_closed_and_finish_preserves_the_error() {
    let (entered_send, entered) = mpsc::sync_channel(1);
    let worker = StreamWorker::spawn(vec![0; 64], options(), move |_frames, _stop| {
        entered_send.send(()).unwrap();
        Err(io::ErrorKind::PermissionDenied.into())
    })
    .unwrap();
    entered.recv_timeout(Duration::from_secs(1)).unwrap();
    until(|| worker.is_finished());
    let mut worker = worker;
    assert_eq!(worker.publish(frame(1)), FramePublish::Closed);
    worker.set_online(true);
    assert_eq!(worker.publish(frame(2)), FramePublish::Closed);
    assert_eq!(
        worker.finish().unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
}
#[test]
fn dropping_handle_requests_stop_and_keeps_worker_owned_resources_alive() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (release_send, release) = mpsc::sync_channel(1);
    let (retired_send, retired) = mpsc::sync_channel(1);
    let worker = StreamWorker::spawn(vec![0; 64], options(), move |_frames, stop| {
        // This models a native owner that cannot be freed until its operation
        // returns. Drop of the capture-side handle must never free it early.
        ready_send.send(()).unwrap();
        release.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(stop.is_requested());
        retired_send.send(()).unwrap();
        Ok(())
    })
    .unwrap();
    ready.recv_timeout(Duration::from_secs(1)).unwrap();
    let started = Instant::now();
    drop(worker);
    assert!(started.elapsed() < Duration::from_millis(200));
    assert!(retired.recv_timeout(Duration::from_millis(20)).is_err());
    release_send.send(()).unwrap();
    retired.recv_timeout(Duration::from_secs(2)).unwrap();
}
#[test]
fn overwritten_handoffs_deliver_only_the_latest_valid_frame() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (release_send, release) = mpsc::sync_channel(1);
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |mut frames, stop| {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        let mut server = Server::new(
            listener,
            vec![0; 64],
            Options {
                max_frame_age_ms: None,
                ..Options::default()
            },
        )?;
        ready_send.send(port).unwrap();
        release.recv_timeout(Duration::from_secs(2)).unwrap();
        frames.serve(&mut server, &stop, || 0)
    })
    .unwrap();
    let port = ready.recv_timeout(Duration::from_secs(1)).unwrap();
    let mut invalid = frame(1);
    invalid.capture.len += 1;
    assert_eq!(
        worker.publish(invalid),
        FramePublish::Rejected(stream_service::RejectReason::LengthMismatch)
    );
    for sequence in 1..10001 {
        assert_eq!(worker.publish(frame(sequence)), FramePublish::Published);
    }
    let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    peer.write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    worker.set_online(true);
    release_send.send(()).unwrap();
    let mut output = Vec::new();
    let mut chunk = [0; 512];
    while !output.ends_with(&[0xff, 0xd9, b'\r', b'\n']) {
        let n = peer.read(&mut chunk).unwrap();
        assert!(n > 0);
        output.extend_from_slice(&chunk[..n]);
        assert!(output.len() <= 1024);
    }
    assert!(output
        .windows(b"X-Sequence: 10000\r\n".len())
        .any(|w| w == b"X-Sequence: 10000\r\n"));
    assert_eq!(
        output
            .windows(b"--nxrs-frame\r\n".len())
            .filter(|w| *w == b"--nxrs-frame\r\n")
            .count(),
        1
    );
    let final_stats = worker.finish().unwrap();
    assert_eq!(final_stats.accepted, 1);
    assert_eq!(final_stats.disconnected, 1);
    assert_eq!(final_stats.frames_completed, 1);
}

#[test]
fn held_owner_control_runs_offline_without_locking_capture_and_stop_prevents_admission() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (entered_send, entered) = mpsc::sync_channel(1);
    let (release_send, release) = mpsc::sync_channel(1);
    let (stats_send, stats) = mpsc::sync_channel(1);
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |mut frames, stop| {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let mut server = Server::new(listener, vec![0; 64], Options::default())?;
        ready_send.send(server.local_addr()?.port()).unwrap();
        let mut entered = Some(entered_send);
        let result = frames.serve_with_control(
            &mut server,
            &stop,
            || 0,
            || {
                if let Some(entered) = entered.take() {
                    entered.send(()).unwrap();
                    release.recv_timeout(Duration::from_secs(2)).unwrap();
                }
                Ok(WorkerAction::Continue)
            },
        );
        stats_send.send(server.stats()).unwrap();
        result
    })
    .unwrap();
    let port = ready.recv_timeout(Duration::from_secs(1)).unwrap();
    entered.recv_timeout(Duration::from_secs(1)).unwrap();
    let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
    peer.write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    let started = Instant::now();
    for sequence in 1..1001 {
        assert_eq!(worker.publish(frame(sequence)), FramePublish::Published);
    }
    assert!(started.elapsed() < Duration::from_millis(300));
    worker.set_online(true);
    worker.request_stop();
    release_send.send(()).unwrap();
    worker.finish().unwrap();
    assert_eq!(
        stats.recv_timeout(Duration::from_secs(1)).unwrap().accepted,
        0
    );
}

#[test]
fn owner_control_failure_disconnects_an_active_stream_and_is_sticky() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (fail_send, fail) = mpsc::sync_channel(1);
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |mut frames, stop| {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let mut server = Server::new(
            listener,
            vec![0; 64],
            Options {
                max_frame_age_ms: None,
                ..Options::default()
            },
        )?;
        ready_send.send(server.local_addr()?.port()).unwrap();
        frames.serve_with_control(
            &mut server,
            &stop,
            || 0,
            || {
                if fail.try_recv().is_ok() {
                    return Err(io::ErrorKind::PermissionDenied.into());
                }
                Ok(WorkerAction::Continue)
            },
        )
    })
    .unwrap();
    let port = ready.recv_timeout(Duration::from_secs(1)).unwrap();
    let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    peer.write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    worker.publish(frame(1));
    worker.set_online(true);
    until(|| worker.stats().is_some_and(|s| s.frames_completed >= 1));
    fail_send.send(()).unwrap();
    until(|| worker.is_finished());
    let stats = worker.stats().unwrap();
    assert_eq!((stats.accepted, stats.disconnected), (1, 1));
    let mut bytes = Vec::new();
    peer.read_to_end(&mut bytes).unwrap();
    assert!(bytes.windows(JPEG.len()).any(|b| b == JPEG));
    worker.set_online(true);
    assert_eq!(worker.publish(frame(2)), FramePublish::Closed);
    assert_eq!(
        worker.finish().unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
}

#[test]
fn owner_disconnect_retires_an_idle_stream_without_closing_fresh_admission() {
    let (ready_send, ready) = mpsc::sync_channel(1);
    let (disconnect_send, disconnect) = mpsc::sync_channel(1);
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |mut frames, stop| {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let mut server = Server::new(
            listener,
            vec![0; 64],
            Options {
                max_frame_age_ms: None,
                ..Options::default()
            },
        )?;
        ready_send.send(server.local_addr()?.port()).unwrap();
        frames.serve_with_control(
            &mut server,
            &stop,
            || 0,
            || {
                Ok(if disconnect.try_recv().is_ok() {
                    WorkerAction::Disconnect
                } else {
                    WorkerAction::Continue
                })
            },
        )
    })
    .unwrap();
    let port = ready.recv_timeout(Duration::from_secs(1)).unwrap();
    let connect = || {
        let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        peer.write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .unwrap();
        peer
    };
    let mut first = connect();
    worker.publish(frame(1));
    worker.set_online(true);
    until(|| worker.stats().is_some_and(|s| s.frames_completed == 1));
    disconnect_send.send(()).unwrap();
    until(|| worker.stats().is_some_and(|s| s.disconnected == 1));
    let mut bytes = Vec::new();
    first.read_to_end(&mut bytes).unwrap();
    assert!(bytes.windows(JPEG.len()).any(|b| b == JPEG));
    assert!(!worker.is_finished());
    assert_eq!(worker.publish(frame(2)), FramePublish::Published);
    let second = connect();
    until(|| worker.stats().is_some_and(|s| s.frames_completed == 2));
    let stats = worker.finish().unwrap();
    assert_eq!((stats.accepted, stats.disconnected), (2, 2));
    drop(second);
}

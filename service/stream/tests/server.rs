use camera::{Capture, Format, Frame, PixelFormat};
use std::cell::Cell;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::rc::Rc;
use std::time::{Duration, Instant};
use stream_service::{Failure, Options, Server, SessionError};

const JPEG: &[u8] = &[0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9];

#[test]
fn invalid_capacity_and_deadlines_fail_at_construction() {
    assert!(Server::new(
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [],
        Options::default()
    )
    .is_err());
    for options in [
        Options {
            send_deadline_ms: 0,
            ..Options::default()
        },
        Options {
            request_timeout_ms: 0,
            ..Options::default()
        },
    ] {
        assert!(Server::new(TcpListener::bind("127.0.0.1:0").unwrap(), [0; 64], options).is_err());
    }
}

fn frame(sequence: u64, timestamp_ms: u64, bytes: &[u8]) -> Frame<'_> {
    Frame {
        sequence,
        bytes,
        capture: Capture {
            format: Format {
                width: 320,
                height: 240,
                pixels: PixelFormat::Jpeg,
            },
            len: bytes.len(),
            timestamp_ms,
        },
    }
}

fn server<B: AsRef<[u8]> + AsMut<[u8]>>(buffer: B) -> Server<B> {
    Server::new(
        TcpListener::bind("127.0.0.1:0").unwrap(),
        buffer,
        Options::default(),
    )
    .unwrap()
}

fn check_time(until: Instant) {
    assert!(
        Instant::now() < until,
        "loopback operation exceeded two seconds"
    );
}

fn connect<B: AsRef<[u8]> + AsMut<[u8]>>(server: &Server<B>) -> TcpStream {
    let stream =
        TcpStream::connect_timeout(&server.local_addr().unwrap(), Duration::from_millis(100))
            .unwrap();
    stream.set_nonblocking(true).unwrap();
    stream.set_nodelay(true).unwrap();
    stream
}

fn send(stream: &mut TcpStream, mut bytes: &[u8], until: Instant) {
    while !bytes.is_empty() {
        check_time(until);
        match stream.write(bytes) {
            Ok(0) => panic!("request write returned zero"),
            Ok(count) => bytes = &bytes[count..],
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(error) => panic!("request write: {error}"),
        }
        std::thread::yield_now();
    }
}

fn drive<B: AsRef<[u8]> + AsMut<[u8]>>(
    server: &mut Server<B>,
    stream: &mut TcpStream,
    now: u64,
    latest: Option<Frame<'_>>,
    output: &mut Vec<u8>,
    until: Instant,
    done: impl Fn(&Server<B>, &[u8]) -> bool,
) {
    loop {
        check_time(until);
        let progress = server.poll(now, latest).unwrap();
        assert!(progress.bytes_written <= 4096);
        let mut bytes = [0; 8192];
        match stream.read(&mut bytes) {
            Ok(count) => output.extend_from_slice(&bytes[..count]),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(error) => panic!("response read: {error}"),
        }
        if done(server, output) {
            return;
        }
        std::thread::yield_now();
    }
}

fn wait<B: AsRef<[u8]> + AsMut<[u8]>>(
    server: &mut Server<B>,
    now: u64,
    until: Instant,
    done: impl Fn(&Server<B>) -> bool,
) {
    while !done(server) {
        check_time(until);
        server.poll(now, None).unwrap();
        std::thread::yield_now();
    }
}

#[test]
fn multipart_bytes_are_exact_and_duplicate_frames_are_not_sent() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let mut stream = connect(&server);
    send(
        &mut stream,
        b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n",
        until,
    );
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut stream,
        100,
        Some(frame(7, 99, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 1 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
    let mut expected = b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n".to_vec();
    expected.extend_from_slice(b"--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: 6\r\nX-Sequence: 7\r\nX-Capture-Ms: 99\r\n\r\n");
    expected.extend_from_slice(JPEG);
    expected.extend_from_slice(b"\r\n");
    assert_eq!(output, expected);
    for _ in 0..16 {
        assert!(
            !server
                .poll(101, Some(frame(7, 99, JPEG)))
                .unwrap()
                .frame_accepted
        );
    }
    assert_eq!(server.stats().frames_completed, 1);
    assert_eq!(server.stats().errors, 0);
}

#[test]
fn clock_reply_has_no_body_and_reports_owner_milliseconds() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let mut stream = connect(&server);
    send(&mut stream, b"GET /clock HTTP/1.1\r\n\r\n", until);
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut stream,
        123456,
        None,
        &mut output,
        until,
        |server, bytes| server.stats().disconnected == 1 && bytes.ends_with(b"\r\n\r\n"),
    );
    assert_eq!(
        output,
        b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 0\r\nX-Clock-Ms: 123456\r\n\r\n"
    );
    assert_eq!(server.stats().errors, 0);
}

#[test]
fn bounded_request_errors_and_static_viewer() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let oversized = [b'x'; 1024];
    let cases: &[(&[u8], &[u8])] = &[
        (
            b"POST /stream HTTP/1.1\r\n\r\n",
            b"HTTP/1.1 400 Bad Request\r\n",
        ),
        (
            b"GET /missing HTTP/1.1\r\n\r\n",
            b"HTTP/1.1 404 Not Found\r\n",
        ),
        (
            b"GET /stream HTTP/1.1\r\nBadHeader\r\n\r\n",
            b"HTTP/1.1 400 Bad Request\r\n",
        ),
        (
            &oversized,
            b"HTTP/1.1 431 Request Header Fields Too Large\r\n",
        ),
    ];
    for (index, (request, expected)) in cases.iter().enumerate() {
        let mut stream = connect(&server);
        send(&mut stream, request, until);
        let mut output = Vec::new();
        drive(
            &mut server,
            &mut stream,
            index as u64,
            None,
            &mut output,
            until,
            |server, bytes| {
                server.stats().disconnected == index as u64 + 1 && bytes.ends_with(b"\r\n\r\n")
            },
        );
        assert!(output.starts_with(expected));
        assert!(output
            .windows(b"Content-Length: 0".len())
            .any(|bytes| bytes == b"Content-Length: 0"));
    }
    assert_eq!(server.stats().errors, 4);
    let mut stream = connect(&server);
    send(&mut stream, b"GET / HTTP/1.0\r\n\r\n", until);
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut stream,
        4,
        None,
        &mut output,
        until,
        |server, bytes| server.stats().disconnected == 5 && bytes.ends_with(b"</html>"),
    );
    let text = std::str::from_utf8(&output).unwrap();
    let (headers, body) = text.split_once("\r\n\r\n").unwrap();
    assert!(headers.contains(&format!("Content-Length: {}", body.len())));
    assert!(body.contains("<img src=\"/stream\">"));
}

struct TrackedBuffer {
    bytes: [u8; 64],
    copies: Rc<Cell<usize>>,
    drops: Rc<Cell<usize>>,
}

impl AsRef<[u8]> for TrackedBuffer {
    fn as_ref(&self) -> &[u8] {
        &self.bytes
    }
}
impl AsMut<[u8]> for TrackedBuffer {
    fn as_mut(&mut self) -> &mut [u8] {
        self.copies.set(self.copies.get() + 1);
        &mut self.bytes
    }
}
impl Drop for TrackedBuffer {
    fn drop(&mut self) {
        self.drops.set(self.drops.get() + 1);
    }
}

#[test]
fn disconnect_returns_the_same_buffer_and_new_viewer_gets_only_latest() {
    let until = Instant::now() + Duration::from_secs(2);
    let copies = Rc::new(Cell::new(0));
    let drops = Rc::new(Cell::new(0));
    let mut server = server(TrackedBuffer {
        bytes: [0; 64],
        copies: copies.clone(),
        drops: drops.clone(),
    });
    let mut first = connect(&server);
    send(&mut first, b"GET /stream HTTP/1.1\r\n\r\n", until);
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut first,
        10,
        Some(frame(1, 10, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 1 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
    drop(first);
    server.disconnect();
    wait(&mut server, 11, until, |server| {
        server.stats().disconnected == 1
    });
    assert_eq!(drops.get(), 0);
    let mut second = connect(&server);
    send(&mut second, b"GET /stream.mjpg HTTP/1.1\r\n\r\n", until);
    output.clear();
    drive(
        &mut server,
        &mut second,
        12,
        Some(frame(9, 12, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 2 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
    let text = String::from_utf8_lossy(&output);
    assert!(text.contains("X-Sequence: 9\r\n"));
    assert!(!text.contains("X-Sequence: 1\r\n"));
    // One mutable-view capacity check at construction, then two frame copies.
    assert_eq!(copies.get(), 3);
    assert_eq!(drops.get(), 0);
    assert_eq!(server.stats().errors, 0);
    drop(server);
    assert_eq!(drops.get(), 1);
}

#[test]
fn request_half_close_does_not_stop_the_response() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let mut stream = connect(&server);
    send(&mut stream, b"GET /stream HTTP/1.1\r\n\r\n", until);
    stream.shutdown(std::net::Shutdown::Write).unwrap();
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut stream,
        10,
        Some(frame(1, 10, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 1 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
    drive(
        &mut server,
        &mut stream,
        11,
        Some(frame(2, 11, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 2 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
    assert_eq!(server.stats().disconnected, 0);
    assert_eq!(server.stats().errors, 0);
}

#[test]
fn extra_connections_are_closed_and_request_timeout_is_bounded() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let mut first = connect(&server);
    send(&mut first, b"GET /stream HTTP/1.1\r\n", until);
    wait(&mut server, 10, until, |server| {
        server.stats().accepted == 1
    });
    let mut extra = connect(&server);
    wait(&mut server, 10, until, |server| {
        server.stats().rejected_busy == 1
    });
    assert_eq!(server.stats().accepted, 1);
    loop {
        check_time(until);
        match extra.read(&mut [0; 1]) {
            Ok(0) => break,
            Err(error) if error.kind() == io::ErrorKind::ConnectionReset => break,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            other => panic!("extra connection was not closed: {other:?}"),
        }
        std::thread::yield_now();
    }
    assert!(!server.poll(2010, None).unwrap().active);
    assert_eq!(server.stats().disconnected, 1);
    assert_eq!(server.stats().errors, 1);
    assert_eq!(server.stats().last_failure, Some(Failure::RequestDeadline));
}

#[test]
fn stale_and_future_frames_are_ignored() {
    let until = Instant::now() + Duration::from_secs(2);
    let mut server = server([0; 64]);
    let mut stream = connect(&server);
    send(&mut stream, b"GET /stream HTTP/1.1\r\n\r\n", until);
    let mut output = Vec::new();
    drive(
        &mut server,
        &mut stream,
        1001,
        Some(frame(1, 0, JPEG)),
        &mut output,
        until,
        |_, bytes| bytes.ends_with(b"\r\n\r\n"),
    );
    for timestamp in [0, 1002] {
        for _ in 0..8 {
            assert!(
                !server
                    .poll(1001, Some(frame(1, timestamp, JPEG)))
                    .unwrap()
                    .frame_accepted
            );
        }
    }
    assert_eq!(server.stats().frames_completed, 0);
    drive(
        &mut server,
        &mut stream,
        1001,
        Some(frame(1, 1001, JPEG)),
        &mut output,
        until,
        |server, bytes| server.stats().frames_completed == 1 && bytes.ends_with(b"\xff\xd9\r\n"),
    );
}

#[test]
fn slow_client_send_deadline_uses_fake_clock_and_recovers() {
    for options in [
        Options {
            send_deadline_ms: 500,
            ..Options::default()
        },
        Options::default(),
    ] {
        let expires = 100 + options.send_deadline_ms;
        let until = Instant::now() + Duration::from_secs(2);
        let mut server = Server::new(
            TcpListener::bind("127.0.0.1:0").unwrap(),
            vec![0; 16384],
            options,
        )
        .unwrap();
        let mut stream = connect(&server);
        send(&mut stream, b"GET /stream HTTP/1.1\r\n\r\n", until);
        let mut bytes = vec![0; 16384];
        bytes[..2].copy_from_slice(&[0xff, 0xd8]);
        bytes[16382..].copy_from_slice(&[0xff, 0xd9]);
        loop {
            check_time(until);
            let progress = server.poll(100, Some(frame(1, 100, &bytes))).unwrap();
            if progress.frame_accepted {
                assert!(progress.bytes_written <= 4096);
                assert_eq!(progress.frames_completed, 0);
                break;
            }
            std::thread::yield_now();
        }
        if options.send_deadline_ms > 500 {
            assert!(server.poll(600, None).unwrap().active);
        }
        assert!(!server.poll(expires, None).unwrap().active);
        assert_eq!(server.stats().disconnected, 1);
        assert_eq!(server.stats().frames_completed, 0);
        assert_eq!(server.stats().errors, 1);
        assert_eq!(
            server.stats().last_failure,
            Some(Failure::Stream(SessionError::DeadlineExpired))
        );
        let mut next = connect(&server);
        send(&mut next, b"GET /clock HTTP/1.1\r\n\r\n", until);
        let mut output = Vec::new();
        drive(
            &mut server,
            &mut next,
            expires + 1,
            None,
            &mut output,
            until,
            |server, bytes| server.stats().disconnected == 2 && bytes.ends_with(b"\r\n\r\n"),
        );
        assert!(
            String::from_utf8_lossy(&output).contains(&format!("X-Clock-Ms: {}\r\n", expires + 1))
        );
    }
}

#[test]
fn transport_factory_is_skipped_for_busy_sockets_and_refusal_recovers() {
    let calls = Rc::new(Cell::new(0));
    let attempts = calls.clone();
    let mut server = Server::with_transport(
        TcpListener::bind("127.0.0.1:0").unwrap(),
        [0; 64],
        Options::default(),
        move |socket| {
            let count = attempts.get() + 1;
            attempts.set(count);
            if count == 1 {
                Err(io::Error::from(io::ErrorKind::PermissionDenied))
            } else {
                Ok(socket)
            }
        },
    )
    .unwrap();
    let first = TcpStream::connect(server.local_addr().unwrap()).unwrap();
    let until = Instant::now() + Duration::from_secs(2);
    while calls.get() < 1 {
        assert!(!server.poll(0, None).unwrap().active);
        check_time(until);
        std::thread::yield_now();
    }
    assert_eq!(calls.get(), 1);
    assert_eq!(
        server.stats().last_failure,
        Some(Failure::Configure(io::ErrorKind::PermissionDenied))
    );
    drop(first);
    let second = TcpStream::connect(server.local_addr().unwrap()).unwrap();
    while calls.get() < 2 {
        server.poll(1, None).unwrap();
        check_time(until);
        std::thread::yield_now();
    }
    assert!(server.poll(1, None).unwrap().active);
    assert_eq!(calls.get(), 2);
    let busy = TcpStream::connect(server.local_addr().unwrap()).unwrap();
    while server.stats().rejected_busy < 1 {
        assert!(server.poll(2, None).unwrap().active);
        check_time(until);
        std::thread::yield_now();
    }
    assert_eq!(calls.get(), 2, "busy socket reached the transport factory");
    assert_eq!(server.stats().rejected_busy, 1);
    server.disconnect();
    drop((second, busy));
    let next = TcpStream::connect(server.local_addr().unwrap()).unwrap();
    while calls.get() < 3 {
        server.poll(3, None).unwrap();
        check_time(until);
        std::thread::yield_now();
    }
    assert!(server.poll(3, None).unwrap().active);
    assert_eq!(calls.get(), 3);
    drop(next);
}

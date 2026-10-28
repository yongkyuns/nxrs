use camera::{Capture, Format, Frame, PixelFormat};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use stream_service::{
    FramePublish, Metadata, Options, RejectReason, Server, StreamWorker, WorkerOptions,
};

const JPEG: &[u8] = &[0xff, 0xd8, 0x11, 0xff, 0xd9];
fn frame(sequence: u64) -> Frame<'static> {
    Frame {
        sequence,
        bytes: JPEG,
        capture: Capture {
            format: Format {
                width: 1,
                height: 1,
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
        poll_interval: Duration::from_millis(1),
    }
}
fn metadata(bytes: &[u8], revision: u64) -> Metadata<'_> {
    Metadata {
        revision,
        content_type: "application/json",
        bytes,
    }
}
fn part(peer: &mut TcpStream) -> Vec<u8> {
    let mut output = Vec::new();
    let mut buffer = [0; 1024];
    while !output.ends_with(&[0xff, 0xd9, b'\r', b'\n']) {
        let n = peer.read(&mut buffer).unwrap();
        assert!(n > 0);
        output.extend_from_slice(&buffer[..n]);
        assert!(output.len() <= 8192);
    }
    output
}
fn contains(bytes: &[u8], text: &[u8]) -> bool {
    bytes.windows(text.len()).any(|value| value == text)
}
fn publish(worker: &mut StreamWorker<Vec<u8>>, sequence: u64, value: Option<Metadata<'_>>) {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match worker.publish_with_metadata(frame(sequence), value) {
            FramePublish::Published => return,
            FramePublish::Busy => assert!(Instant::now() < deadline),
            other => panic!("unexpected handoff {other:?}"),
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}
#[test]
fn owned_publications_replace_atomically_and_preserve_legacy_jpeg_sessions() {
    for path in ["/stream?metadata=1", "/stream"] {
        let (ready_send, ready) = mpsc::sync_channel(1);
        let (release_send, release) = mpsc::sync_channel(1);
        let mut worker = StreamWorker::spawn_with_metadata(
            vec![0; 64],
            vec![0; 64].into_boxed_slice(),
            options(),
            move |mut frames, stop| {
                let listener = TcpListener::bind("127.0.0.1:0")?;
                ready_send.send(listener.local_addr()?.port()).unwrap();
                let mut server = Server::new(
                    listener,
                    vec![0; 64],
                    Options {
                        max_frame_age_ms: None,
                        ..Options::default()
                    },
                )?;
                release.recv_timeout(Duration::from_secs(2)).unwrap();
                frames.serve(&mut server, &stop, || 0)
            },
        )
        .unwrap();
        assert_eq!(worker.metadata_capacity(), 64);
        let port = ready.recv_timeout(Duration::from_secs(2)).unwrap();
        let mut bytes = *b"owned";
        publish(&mut worker, 1, Some(metadata(&bytes, 1)));
        bytes.fill(b'x');
        for value in [
            metadata(&[0; 65], 2),
            Metadata {
                content_type: "text/plain\r\nInjected",
                ..metadata(b"bad", 2)
            },
        ] {
            assert_eq!(
                worker.publish_with_metadata(frame(2), Some(value)),
                FramePublish::Rejected(RejectReason::InvalidMetadata)
            );
        }
        let mut invalid = frame(2);
        invalid.capture.len += 1;
        assert_eq!(
            worker.publish_with_metadata(invalid, Some(metadata(b"bad", 2))),
            FramePublish::Rejected(RejectReason::LengthMismatch)
        );
        let mut peer = TcpStream::connect(("127.0.0.1", port)).unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        peer.write_all(format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n").as_bytes())
            .unwrap();
        worker.set_online(true);
        release_send.send(()).unwrap();
        let first = part(&mut peer);
        assert!(contains(&first, b"X-Sequence: 1\r\n"));
        assert!(!contains(&first, b"xxxxx") && !contains(&first, b"bad"));
        assert_eq!(contains(&first, b"\r\nowned\r\n"), path.ends_with("=1"));
        publish(&mut worker, 3, Some(metadata(b"next", 2)));
        let second = part(&mut peer);
        assert!(contains(&second, b"X-Sequence: 3\r\n"));
        assert_eq!(contains(&second, b"\r\nnext\r\n"), path.ends_with("=1"));
        publish(&mut worker, 4, None);
        let third = part(&mut peer);
        assert!(contains(&third, b"X-Sequence: 4\r\n"));
        assert!(!contains(&third, b"application/json") && !contains(&third, b"next"));
        publish(&mut worker, 5, Some(metadata(b"same-revision", 2)));
        let fourth = part(&mut peer);
        assert!(contains(&fourth, b"X-Sequence: 5\r\n"));
        assert!(!contains(&fourth, b"application/json"));
        worker.finish().unwrap();
    }
}
#[test]
fn metadata_requires_explicit_bounded_storage_before_starting_a_worker() {
    for length in [0, stream_service::MAX_METADATA_BYTES + 1] {
        assert!(StreamWorker::spawn_with_metadata(
            vec![0; 64],
            vec![0; length].into_boxed_slice(),
            options(),
            |_, _| panic!("invalid storage started worker")
        )
        .is_err());
    }
    let (release_send, release) = mpsc::sync_channel(1);
    let mut worker = StreamWorker::spawn(vec![0; 64], options(), move |_, _| {
        release.recv_timeout(Duration::from_secs(2)).unwrap();
        Ok(())
    })
    .unwrap();
    assert_eq!(worker.metadata_capacity(), 0);
    assert_eq!(
        worker.publish_with_metadata(frame(1), Some(metadata(b"value", 1))),
        FramePublish::Rejected(RejectReason::InvalidMetadata)
    );
    release_send.send(()).unwrap();
    worker.finish().unwrap();
}

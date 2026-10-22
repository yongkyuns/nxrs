use camera::{Capture, Format, Frame, PixelFormat};
use std::io::{self, Write};
use stream_service::{MjpegSession, OfferResult, RejectReason, SessionError};

#[derive(Clone, Copy)]
enum Step {
    Write(usize),
    InvalidCount,
    Error(io::ErrorKind),
}

#[derive(Default)]
struct FakeWriter {
    output: Vec<u8>,
    steps: Vec<Step>,
    calls: usize,
    largest_write: usize,
}

impl Write for FakeWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.calls += 1;
        self.largest_write = self.largest_write.max(bytes.len());
        if !self.steps.is_empty() {
            match self.steps.remove(0) {
                Step::Write(0) => return Ok(0),
                Step::Write(limit) => {
                    let count = limit.min(bytes.len());
                    self.output.extend_from_slice(&bytes[..count]);
                    return Ok(count);
                }
                Step::InvalidCount => return Ok(bytes.len() + 1),
                Step::Error(kind) => return Err(io::Error::from(kind)),
            }
        }
        self.output.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

const JPEG_A: &[u8] = &[0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9];
const JPEG_B: &[u8] = &[0xff, 0xd8, 0x33, 0xff, 0xd9];
const RESPONSE: &[u8] = b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n";

#[test]
fn metadata_is_copied_and_framed_before_its_jpeg_under_partial_writes() {
    let mut s = session();
    finish_response(&mut s);
    let mut json = b"{\"ok\":1}".to_vec();
    assert_eq!(
        s.offer_with_metadata(
            frame(1, 1, JPEG_A),
            1,
            Some(stream_service::Metadata {
                revision: 1,
                content_type: "application/json",
                bytes: &json
            })
        ),
        OfferResult::Accepted
    );
    json.fill(b'x');
    while !s.poll(2, 1).unwrap().ready {}
    let (w, _, stats) = s.into_parts();
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(b"--nxrs-frame\r\nContent-Type: application/json\r\nContent-Length: 8\r\n\r\n{\"ok\":1}\r\n");
    expected.extend(expected_part(1, 1, JPEG_A));
    assert_eq!(w.output, expected);
    assert_eq!(stats.completed, 1);
}
#[test]
fn metadata_capacity_and_header_injection_fail_without_corrupting_the_session() {
    let mut s = session();
    finish_response(&mut s);
    let big = vec![0; stream_service::MAX_METADATA_BYTES + 1];
    for (kind, bytes) in [
        ("application/json", big.as_slice()),
        ("application/json\r\nInjected: yes", b"{}".as_slice()),
    ] {
        assert_eq!(
            s.offer_with_metadata(
                frame(1, 1, JPEG_A),
                1,
                Some(stream_service::Metadata {
                    revision: 1,
                    content_type: kind,
                    bytes
                })
            ),
            OfferResult::Rejected(RejectReason::InvalidMetadata)
        );
    }
    assert_eq!(s.offer(frame(1, 1, JPEG_A), 1), OfferResult::Accepted);
    drain(&mut s, 2);
}

fn expected_part(sequence: u64, timestamp_ms: u64, bytes: &[u8]) -> Vec<u8> {
    let mut part = format!(
        "--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\nX-Sequence: {sequence}\r\nX-Capture-Ms: {timestamp_ms}\r\n\r\n",
        bytes.len()
    ).into_bytes();
    part.extend_from_slice(bytes);
    part.extend_from_slice(b"\r\n");
    part
}

fn frame(sequence: u64, timestamp_ms: u64, bytes: &[u8]) -> Frame<'_> {
    Frame {
        sequence,
        capture: Capture {
            format: Format {
                width: 640,
                height: 480,
                pixels: PixelFormat::Jpeg,
            },
            len: bytes.len(),
            timestamp_ms,
        },
        bytes,
    }
}

fn session() -> MjpegSession<FakeWriter, Vec<u8>> {
    MjpegSession::new(FakeWriter::default(), vec![0; 16], 0, 100)
}

fn finish_response(session: &mut MjpegSession<FakeWriter, Vec<u8>>) {
    while !session.poll(0, 1024).unwrap().ready {}
}

fn drain(session: &mut MjpegSession<FakeWriter, Vec<u8>>, now_ms: u64) {
    while !session.poll(now_ms, 1024).unwrap().ready {}
}

#[test]
fn writes_exact_multipart_framing_and_actual_length() {
    let mut session = session();
    finish_response(&mut session);
    assert_eq!(
        session.offer(frame(42, 9876, JPEG_A), 1),
        OfferResult::Accepted
    );
    drain(&mut session, 2);
    let (writer, _, stats) = session.into_parts();
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(b"--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: 6\r\nX-Sequence: 42\r\nX-Capture-Ms: 9876\r\n\r\n");
    expected.extend_from_slice(JPEG_A);
    expected.extend_from_slice(b"\r\n");
    assert_eq!(writer.output, expected);
    assert_eq!(stats.accepted, 1);
    assert_eq!(stats.completed, 1);
    assert_eq!(stats.bytes_written as usize, expected.len());
}

#[test]
fn coalesces_header_payload_and_tail_into_one_write() {
    let mut session = session();
    finish_response(&mut session);
    assert_eq!(
        session.offer(frame(42, 9876, JPEG_A), 1),
        OfferResult::Accepted
    );
    let part = expected_part(42, 9876, JPEG_A);
    let progress = session.poll(2, usize::MAX).unwrap();
    assert_eq!(progress.calls, 1);
    assert_eq!(progress.bytes, part.len());
    assert!(progress.ready);
    let (writer, buffer, stats) = session.into_parts();
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(&part);
    assert_eq!(writer.output, expected);
    assert_eq!(buffer.len(), 16);
    assert_eq!(stats.completed, 1);
}

#[test]
fn partial_batches_cross_segment_boundaries_without_completing_early() {
    let part = expected_part(1, 10, JPEG_A);
    let header_len = part.len() - JPEG_A.len() - 2;
    let writer = FakeWriter {
        steps: vec![
            Step::Write(usize::MAX), // HTTP response.
            Step::Write(header_len - 1),
            Step::Error(io::ErrorKind::WouldBlock),
            Step::Write(2), // Last header byte and first JPEG byte.
            Step::Error(io::ErrorKind::Interrupted),
            Step::Error(io::ErrorKind::WouldBlock),
            Step::Write(JPEG_A.len()), // Rest of JPEG and first trailer byte.
            Step::Error(io::ErrorKind::WouldBlock),
            Step::Write(1),
        ],
        ..FakeWriter::default()
    };
    let mut session = MjpegSession::new(writer, vec![0; 16], 0, 100);
    finish_response(&mut session);
    assert_eq!(
        session.offer(frame(1, 10, JPEG_A), 1),
        OfferResult::Accepted
    );
    let first = session.poll(2, 1024).unwrap();
    assert_eq!(
        (first.calls, first.bytes, first.ready),
        (2, header_len - 1, false)
    );
    assert_eq!(session.offer(frame(2, 11, JPEG_B), 2), OfferResult::Busy);
    let second = session.poll(3, 1024).unwrap();
    assert_eq!((second.calls, second.bytes, second.ready), (3, 2, false));
    let third = session.poll(4, 1024).unwrap();
    assert_eq!(
        (third.calls, third.bytes, third.ready),
        (2, JPEG_A.len(), false)
    );
    assert_eq!(session.stats().completed, 0);
    let last = session.poll(5, 1024).unwrap();
    assert_eq!((last.calls, last.bytes, last.ready), (1, 1, true));
    let (writer, _, stats) = session.into_parts();
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(&part);
    assert_eq!(writer.output, expected);
    assert_eq!(stats.completed, 1);
    assert_eq!(stats.busy, 1);
    assert_eq!(stats.bytes_written as usize, expected.len());
}

#[test]
fn batches_remain_bounded_by_stack_scratch_and_eight_calls() {
    let mut jpeg = vec![0x55; 2048 * 8];
    jpeg[..2].copy_from_slice(&[0xff, 0xd8]);
    let length = jpeg.len();
    jpeg[length - 2..].copy_from_slice(&[0xff, 0xd9]);
    let mut session = MjpegSession::new(FakeWriter::default(), vec![0; length], 0, 100);
    finish_response(&mut session);
    assert_eq!(session.offer(frame(1, 1, &jpeg), 1), OfferResult::Accepted);
    let progress = session.poll(2, usize::MAX).unwrap();
    assert_eq!(
        (progress.calls, progress.bytes, progress.ready),
        (8, 2048 * 8, false)
    );
    assert_eq!(session.stats().completed, 0);
    drain(&mut session, 3);
    let (writer, buffer, stats) = session.into_parts();
    assert_eq!(writer.largest_write, 2048);
    assert_eq!(buffer.len(), length);
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(&expected_part(1, 1, &jpeg));
    assert_eq!(writer.output, expected);
    assert_eq!(stats.completed, 1);
}

#[test]
fn every_byte_budget_preserves_concatenated_protocol_bytes() {
    let mut jpeg = vec![0x55; 2048 + JPEG_B.len()];
    jpeg[..2].copy_from_slice(&[0xff, 0xd8]);
    let length = jpeg.len();
    jpeg[length - 2..].copy_from_slice(&[0xff, 0xd9]);
    let mut expected = RESPONSE.to_vec();
    expected.extend_from_slice(&expected_part(42, 9876, JPEG_A));
    expected.extend_from_slice(&expected_part(u64::MAX, u64::MAX, &jpeg));
    for budget in 1..=expected.len() {
        let mut session = MjpegSession::new(FakeWriter::default(), vec![0; length], 0, 100);
        let zero = session.poll(0, 0).unwrap();
        assert_eq!((zero.calls, zero.bytes, zero.ready), (0, 0, false));
        for (sequence, capture_ms, bytes, now_ms) in [
            (42, 9876, JPEG_A, 1),
            (u64::MAX, u64::MAX, jpeg.as_slice(), 2),
        ] {
            loop {
                let progress = session.poll(now_ms, budget).unwrap();
                assert!(progress.bytes <= budget);
                assert!(progress.calls <= 8);
                if progress.ready {
                    break;
                }
                assert!(progress.bytes > 0);
            }
            assert_eq!(
                session.offer(frame(sequence, capture_ms, bytes), now_ms),
                OfferResult::Accepted
            );
            let before = session.stats();
            let zero = session.poll(now_ms, 0).unwrap();
            assert_eq!((zero.calls, zero.bytes, zero.ready), (0, 0, false));
            assert_eq!(session.stats(), before);
        }
        while !session.poll(2, budget).unwrap().ready {}
        let (writer, _, stats) = session.into_parts();
        assert_eq!(writer.output, expected, "byte budget {budget}");
        assert_eq!(stats.completed, 2);
        assert_eq!(stats.bytes_written as usize, expected.len());
    }
}

#[test]
fn invalid_count_cannot_commit_a_speculative_batch() {
    let writer = FakeWriter {
        steps: vec![Step::Write(usize::MAX), Step::InvalidCount],
        ..FakeWriter::default()
    };
    let mut session = MjpegSession::new(writer, vec![0; 16], 0, 100);
    finish_response(&mut session);
    assert_eq!(session.offer(frame(1, 1, JPEG_A), 1), OfferResult::Accepted);
    assert_eq!(session.poll(2, 1024), Err(SessionError::InvalidWriteCount));
    let (writer, _, stats) = session.into_parts();
    assert_eq!(writer.output, RESPONSE);
    assert_eq!(stats.completed, 0);
    assert_eq!(stats.failures, 1);
    assert_eq!(stats.bytes_written as usize, RESPONSE.len());
}

#[test]
fn handles_partial_would_block_and_interrupted_writes() {
    let writer = FakeWriter {
        steps: vec![
            Step::Write(3),
            Step::Error(io::ErrorKind::WouldBlock),
            Step::Error(io::ErrorKind::Interrupted),
            Step::Write(2),
        ],
        ..FakeWriter::default()
    };
    let mut session = MjpegSession::new(writer, vec![0; 16], 0, 100);
    let first = session.poll(0, 1000).unwrap();
    assert_eq!(first.bytes, 3);
    assert_eq!(first.calls, 2);
    let second = session.poll(0, 2).unwrap();
    assert_eq!(second.bytes, 2);
    assert_eq!(second.calls, 2);
    drain(&mut session, 0);
    let (writer, _, _) = session.into_parts();
    assert!(writer.output.starts_with(b"HTTP/1.1 200 OK"));
}

#[test]
fn byte_budget_and_eintr_call_limit_are_enforced() {
    let mut budgeted = session();
    let result = budgeted.poll(0, 7).unwrap();
    assert_eq!(result.bytes, 7);
    assert_eq!(result.calls, 1);

    let steps = (0..20)
        .map(|_| Step::Error(io::ErrorKind::Interrupted))
        .collect();
    let mut session = MjpegSession::new(
        FakeWriter {
            steps,
            ..FakeWriter::default()
        },
        vec![0; 8],
        0,
        100,
    );
    let result = session.poll(0, 7).unwrap();
    assert_eq!(result.calls, 8);
    assert_eq!(result.bytes, 0);
}

#[test]
fn busy_offer_cannot_overwrite_inflight_frame() {
    let mut session = session();
    finish_response(&mut session);
    assert_eq!(
        session.offer(frame(1, 10, JPEG_A), 1),
        OfferResult::Accepted
    );
    assert_eq!(session.offer(frame(2, 11, JPEG_B), 1), OfferResult::Busy);
    drain(&mut session, 2);
    let (writer, _, stats) = session.into_parts();
    assert!(writer
        .output
        .ends_with(&[0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9, b'\r', b'\n']));
    assert_eq!(stats.busy, 1);
    assert_eq!(stats.skipped, 1);
}

#[test]
fn rejects_malformed_or_oversized_frames() {
    let mut session = session();
    finish_response(&mut session);
    assert_eq!(
        session.offer(frame(1, 1, &[1, 2, 3, 4]), 1),
        OfferResult::Rejected(RejectReason::InvalidMarkers)
    );
    assert_eq!(
        session.offer(frame(2, 1, &[0xff, 0xd8, 0xff, 0xd9]), 1),
        OfferResult::Accepted
    );
    drain(&mut session, 2);
    let large = [
        0xff, 0xd8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xd9,
    ];
    assert_eq!(
        session.offer(frame(3, 3, &large), 3),
        OfferResult::Rejected(RejectReason::TooLarge)
    );
    let mut mismatch = frame(4, 4, JPEG_A);
    mismatch.capture.len -= 1;
    assert_eq!(
        session.offer(mismatch, 4),
        OfferResult::Rejected(RejectReason::LengthMismatch)
    );
}

#[test]
fn deadline_closes_instead_of_sending_a_later_part() {
    let mut response = MjpegSession::new(FakeWriter::default(), vec![0; 16], 10, 5);
    assert_eq!(response.poll(15, 1), Err(SessionError::DeadlineExpired));
    assert_eq!(response.stats().failures, 1);

    let mut session = session();
    finish_response(&mut session);
    assert_eq!(session.offer(frame(1, 1, JPEG_A), 1), OfferResult::Accepted);
    assert_eq!(session.poll(101, 1024), Err(SessionError::DeadlineExpired));
    assert_eq!(session.offer(frame(2, 2, JPEG_B), 101), OfferResult::Closed);
}

#[test]
fn backwards_clock_closes_session() {
    let mut session = MjpegSession::new(FakeWriter::default(), vec![0; 8], 5, 100);
    assert_eq!(session.poll(4, 1), Err(SessionError::ClockWentBackwards));
    assert_eq!(session.poll(6, 1), Err(SessionError::Closed));
}

#[test]
fn zero_and_fatal_writes_close_the_session() {
    let mut zero = MjpegSession::new(
        FakeWriter {
            steps: vec![Step::Write(0)],
            ..FakeWriter::default()
        },
        vec![],
        0,
        100,
    );
    assert_eq!(zero.poll(0, 10), Err(SessionError::WriteZero));

    let mut failed = MjpegSession::new(
        FakeWriter {
            steps: vec![Step::Error(io::ErrorKind::BrokenPipe)],
            ..FakeWriter::default()
        },
        vec![],
        0,
        100,
    );
    assert_eq!(
        failed.poll(0, 10),
        Err(SessionError::Write(io::ErrorKind::BrokenPipe))
    );
}

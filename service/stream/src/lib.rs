//! Bounded, portable multipart MJPEG session writing.
#![forbid(unsafe_code)]
mod server;
mod worker;
pub use server::{Failure, Options, Progress, Server, ServerStats};
pub use worker::{FramePublish, StreamWorker, WorkerOptions, WorkerReceiver, WorkerStop};

use camera::{Frame, PixelFormat};
use core::fmt;
use std::io::{self, Write};

const RESPONSE: &[u8] = b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n";
const MAX_WRITE_CALLS: u8 = 8;
const WRITE_BATCH_BYTES: usize = 2048;
const PART_PREFIX: &[u8] = b"--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: ";
const SEQUENCE_PREFIX: &[u8] = b"\r\nX-Sequence: ";
const CAPTURE_PREFIX: &[u8] = b"\r\nX-Capture-Ms: ";
const PART_END: &[u8] = b"\r\n\r\n";
const PART_TAIL: &[u8] = b"\r\n";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SessionStats {
    pub accepted: u64,
    pub completed: u64,
    pub busy: u64,
    pub skipped: u64,
    pub bytes_written: u64,
    pub failures: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RejectReason {
    NotJpeg,
    InvalidDimensions,
    LengthMismatch,
    TooLarge,
    InvalidMarkers,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OfferResult {
    Accepted,
    Busy,
    Rejected(RejectReason),
    Closed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionError {
    Closed,
    DeadlineExpired,
    ClockWentBackwards,
    WriteZero,
    Write(io::ErrorKind),
    InvalidWriteCount,
}

impl fmt::Display for SessionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for SessionError {}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PollProgress {
    pub calls: u8,
    pub bytes: usize,
    pub ready: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    Response(usize),
    Ready,
    PartHeader { position: usize, length: usize },
    Payload { position: usize, length: usize },
    Tail(usize),
    Closed,
}

/// One-client multipart MJPEG output state. The caller owns scheduling, the
/// monotonic clock, and the writer's socket/nonblocking policy.
pub struct MjpegSession<W, B> {
    writer: W,
    buffer: B,
    state: State,
    part_header: [u8; 192],
    payload_len: usize,
    deadline_ms: Option<u64>,
    timeout_ms: u64,
    last_now_ms: u64,
    stats: SessionStats,
}

impl<W: Write, B: AsRef<[u8]> + AsMut<[u8]>> MjpegSession<W, B> {
    /// Queue the HTTP response immediately. Its header has the same bounded
    /// send deadline as a frame, measured from `now_ms`.
    pub fn new(writer: W, buffer: B, now_ms: u64, send_deadline_ms: u64) -> Self {
        Self {
            writer,
            buffer,
            state: State::Response(0),
            part_header: [0; 192],
            payload_len: 0,
            deadline_ms: Some(now_ms.saturating_add(send_deadline_ms)),
            timeout_ms: send_deadline_ms,
            last_now_ms: now_ms,
            stats: SessionStats::default(),
        }
    }

    pub fn stats(&self) -> SessionStats {
        self.stats
    }

    /// Copy a valid JPEG into the single owned frame buffer only while ready.
    /// Busy/rejected offers are skipped; the caller may offer its latest frame
    /// after the current part has completed.
    pub fn offer(&mut self, frame: Frame<'_>, now_ms: u64) -> OfferResult {
        if self.state == State::Closed {
            return OfferResult::Closed;
        }
        if self.check_clock(now_ms).is_err() {
            return OfferResult::Closed;
        }
        if self.deadline_ms.is_some_and(|deadline| now_ms >= deadline) {
            let _ = self.fail::<()>(SessionError::DeadlineExpired);
            return OfferResult::Closed;
        }
        if self.state != State::Ready {
            self.stats.busy = self.stats.busy.saturating_add(1);
            self.stats.skipped = self.stats.skipped.saturating_add(1);
            return OfferResult::Busy;
        }
        let reason = reject_frame(&frame, self.buffer.as_ref().len());
        if let Some(reason) = reason {
            self.stats.skipped = self.stats.skipped.saturating_add(1);
            return OfferResult::Rejected(reason);
        }

        self.buffer.as_mut()[..frame.bytes.len()].copy_from_slice(frame.bytes);
        self.payload_len = frame.bytes.len();
        let length = write_part_header(
            &mut self.part_header,
            frame.bytes.len(),
            frame.sequence,
            frame.capture.timestamp_ms,
        );
        self.state = State::PartHeader {
            position: 0,
            length,
        };
        self.deadline_ms = Some(now_ms.saturating_add(self.timeout_ms));
        self.stats.accepted = self.stats.accepted.saturating_add(1);
        OfferResult::Accepted
    }

    /// Write at most `byte_budget` bytes and eight calls. `WouldBlock` and
    /// `Interrupted` preserve the pending offset. Timeout or fatal errors close
    /// the session so a partial multipart part is never followed by another.
    pub fn poll(&mut self, now_ms: u64, byte_budget: usize) -> Result<PollProgress, SessionError> {
        if self.state == State::Closed {
            return Err(SessionError::Closed);
        }
        self.check_clock(now_ms)?;
        if self.deadline_ms.is_some_and(|deadline| now_ms >= deadline) {
            return self.fail(SessionError::DeadlineExpired);
        }

        let mut progress = PollProgress::default();
        let mut batch = [0u8; WRITE_BATCH_BYTES];
        while progress.calls < MAX_WRITE_CALLS && progress.bytes < byte_budget {
            let remaining = (byte_budget - progress.bytes).min(batch.len());
            let length = self.fill_batch(&mut batch[..remaining]);
            if length == 0 {
                break;
            }
            progress.calls += 1;
            match self.writer.write(&batch[..length]) {
                Ok(0) => return self.fail(SessionError::WriteZero),
                Ok(count) if count <= length => {
                    progress.bytes += count;
                    self.stats.bytes_written =
                        self.stats.bytes_written.saturating_add(count as u64);
                    self.advance(count);
                }
                Ok(_) => return self.fail(SessionError::InvalidWriteCount),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) => return self.fail(SessionError::Write(error.kind())),
            }
        }
        progress.ready = self.state == State::Ready;
        Ok(progress)
    }

    pub fn into_parts(self) -> (W, B, SessionStats) {
        (self.writer, self.buffer, self.stats)
    }

    pub(crate) fn writer_mut(&mut self) -> &mut W {
        &mut self.writer
    }

    fn segment(&self, state: State) -> Option<(&[u8], usize)> {
        match state {
            State::Response(position) => Some((RESPONSE, position)),
            State::PartHeader { position, length } => Some((&self.part_header[..length], position)),
            State::Payload { position, length } => {
                Some((&self.buffer.as_ref()[..length], position))
            }
            State::Tail(position) => Some((PART_TAIL, position)),
            State::Ready | State::Closed => None,
        }
    }

    fn fill_batch(&self, output: &mut [u8]) -> usize {
        // Look ahead without committing offsets, deadlines, or completion stats.
        let mut state = self.state;
        let mut filled = 0;
        while filled < output.len() {
            let Some((segment, position)) = self.segment(state) else {
                break;
            };
            let count = (segment.len() - position).min(output.len() - filled);
            output[filled..filled + count].copy_from_slice(&segment[position..position + count]);
            filled += count;
            state = self.next_state(state, position + count, segment.len());
        }
        filled
    }

    fn advance(&mut self, mut bytes: usize) {
        while bytes > 0 {
            // The writer's count was bounded by fill_batch's pending bytes.
            let (segment, position) = self.segment(self.state).expect("pending write batch");
            let segment_length = segment.len();
            let count = bytes.min(segment_length - position);
            let next = self.next_state(self.state, position + count, segment_length);
            if next == State::Ready {
                if matches!(self.state, State::Tail(_)) {
                    self.stats.completed = self.stats.completed.saturating_add(1);
                }
                self.deadline_ms = None;
            }
            self.state = next;
            bytes -= count;
        }
    }

    fn next_state(&self, state: State, position: usize, segment_length: usize) -> State {
        if position != segment_length {
            return match state {
                State::Response(_) => State::Response(position),
                State::PartHeader { length, .. } => State::PartHeader { position, length },
                State::Payload { length, .. } => State::Payload { position, length },
                State::Tail(_) => State::Tail(position),
                state => state,
            };
        }
        match state {
            State::Response(_) | State::Tail(_) => State::Ready,
            State::PartHeader { .. } => State::Payload {
                position: 0,
                length: self.payload_len,
            },
            State::Payload { .. } => State::Tail(0),
            state => state,
        }
    }

    fn check_clock(&mut self, now_ms: u64) -> Result<(), SessionError> {
        if now_ms < self.last_now_ms {
            return self.fail(SessionError::ClockWentBackwards);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }

    fn fail<T>(&mut self, error: SessionError) -> Result<T, SessionError> {
        self.state = State::Closed;
        self.deadline_ms = None;
        self.stats.failures = self.stats.failures.saturating_add(1);
        Err(error)
    }
}

fn write_part_header(
    output: &mut [u8; 192],
    length: usize,
    sequence: u64,
    capture_ms: u64,
) -> usize {
    let mut cursor = 0;
    append(output, &mut cursor, PART_PREFIX);
    append_number(output, &mut cursor, length as u64);
    append(output, &mut cursor, SEQUENCE_PREFIX);
    append_number(output, &mut cursor, sequence);
    append(output, &mut cursor, CAPTURE_PREFIX);
    append_number(output, &mut cursor, capture_ms);
    append(output, &mut cursor, PART_END);
    cursor
}

fn append(output: &mut [u8], cursor: &mut usize, bytes: &[u8]) {
    let end = *cursor + bytes.len();
    output[*cursor..end].copy_from_slice(bytes);
    *cursor = end;
}

fn append_number(output: &mut [u8], cursor: &mut usize, mut value: u64) {
    let start = *cursor;
    let mut digits = [0u8; 20];
    let mut count = 0;
    loop {
        digits[count] = b'0' + (value % 10) as u8;
        count += 1;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    while count > 0 {
        count -= 1;
        output[*cursor] = digits[count];
        *cursor += 1;
    }
    debug_assert!(*cursor - start <= 20);
}

// Both the outgoing session and the capture/worker exchange intentionally use
// this same bounded JPEG frame contract.
fn reject_frame(frame: &Frame<'_>, capacity: usize) -> Option<RejectReason> {
    if frame.capture.format.pixels != PixelFormat::Jpeg {
        Some(RejectReason::NotJpeg)
    } else if frame.capture.format.width == 0 || frame.capture.format.height == 0 {
        Some(RejectReason::InvalidDimensions)
    } else if frame.capture.len != frame.bytes.len() {
        Some(RejectReason::LengthMismatch)
    } else if frame.bytes.len() > capacity {
        Some(RejectReason::TooLarge)
    } else if frame.bytes.len() < 4
        || frame.bytes[0..2] != [0xff, 0xd8]
        || frame.bytes[frame.bytes.len() - 2..] != [0xff, 0xd9]
    {
        Some(RejectReason::InvalidMarkers)
    } else {
        None
    }
}

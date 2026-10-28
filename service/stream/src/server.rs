//! Caller-polled HTTP server with one active client. Extra connections are
//! accepted and immediately closed; no queue or background worker is created.

use crate::{append, append_number, Metadata, MjpegSession, OfferResult, SessionError};
use camera::Frame;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};

const REQUEST_CAPACITY: usize = 1024;
const WRITE_BUDGET: usize = 4096;
// An inline empty icon avoids a second browser request competing for the
// deliberately single-client server while the image connection is admitted.
const VIEWER: &[u8] = b"<!doctype html><html><head><link rel=\"icon\" href=\"data:,\"></head><body><img src=\"/stream\"></body></html>";

#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// Absolute request deadline from accept; defaults to 2000 milliseconds.
    pub request_timeout_ms: u64,
    /// Whole-response/whole-frame send deadline; defaults to 3000 milliseconds.
    pub send_deadline_ms: u64,
    /// Capture timestamps must share the owner clock. None disables the age cap.
    pub max_frame_age_ms: Option<u64>,
    /// Maximum time without output progress. None disables the idle timeout.
    pub idle_timeout_ms: Option<u64>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            request_timeout_ms: 2000,
            // A sub-second deadline can abort before TCP's first retransmit.
            // The pending frame stays bounded while a normal retry is allowed.
            send_deadline_ms: 3000,
            max_frame_age_ms: Some(1000),
            idle_timeout_ms: Some(5000),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ServerStats {
    /// Admitted clients, including HTTP requests that do not start a stream.
    pub accepted: u64,
    /// Closed admitted clients; busy rejections are counted separately.
    pub disconnected: u64,
    pub rejected_busy: u64,
    pub frames_completed: u64,
    /// HTTP rejections, timeouts, clock violations, and connection failures.
    pub errors: u64,
    /// Most recent failure, retained across reconnects without allocating.
    pub last_failure: Option<Failure>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Clock,
    RequestDeadline,
    ResponseDeadline,
    IdleDeadline,
    InvalidRequest,
    InvalidFrame,
    UnexpectedInput,
    Configure(io::ErrorKind),
    Read(io::ErrorKind),
    Write(io::ErrorKind),
    Stream(SessionError),
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Progress {
    pub active: bool,
    pub bytes_written: usize,
    pub frame_accepted: bool,
    pub frames_completed: u64,
}

struct Request<S> {
    socket: S,
    length: usize,
    deadline: u64,
}

struct Reply<S> {
    socket: S,
    bytes: [u8; 256],
    length: usize,
    position: usize,
    deadline: u64,
}

struct Streaming<B, S> {
    session: MjpegSession<S, B>,
    read_closed: bool,
    last_offered: u64,
    last_write_ms: u64,
    ready: bool,
    metadata_enabled: bool,
    metadata_revision: u64,
}

enum Client<B, S> {
    Request(Request<S>),
    Reply(Reply<S>),
    Streaming(Streaming<B, S>),
}
#[derive(Clone, Copy)]
enum ReplyKind {
    Clock,
    Viewer,
    Error(&'static [u8]),
}

/// A single-viewer server. `poll` attempts one accept, at most one read (1024
/// bytes for requests, one byte for stream EOF detection), and one bounded
/// output step. The owner supplies the latest frame on every poll; no borrow
/// survives the call. Only listener errors escape through the poll result.
pub struct Server<B, S = TcpStream, F = fn(TcpStream) -> io::Result<S>> {
    listener: TcpListener,
    transport: F,
    buffer: Option<B>,
    header: Option<Box<[u8]>>,
    client: Option<Client<B, S>>,
    options: Options,
    stats: ServerStats,
    last_now_ms: Option<u64>,
    // Keep the bounded request storage in one place. Moving the client state
    // between poll steps must not copy a KiB array onto nested task stacks.
    request_bytes: [u8; REQUEST_CAPACITY],
}

impl<B: AsRef<[u8]> + AsMut<[u8]>> Server<B> {
    /// Explicit cleartext listener, intended for development or a separately
    /// protected deployment boundary. Authenticated transports use with_transport.
    pub fn new(listener: TcpListener, buffer: B, options: Options) -> io::Result<Self> {
        Self::with_transport(listener, buffer, options, Ok)
    }
}

impl<B, S, F> Server<B, S, F>
where
    B: AsRef<[u8]> + AsMut<[u8]>,
    S: Read + Write,
    F: FnMut(TcpStream) -> io::Result<S>,
{
    /// Wrap each admitted nonblocking TCP socket in the caller's owned transport.
    /// Busy connections are dropped before this factory runs. The transport
    /// must make bounded progress on Read/Write, returning WouldBlock while its
    /// handshake is pending, and admit no application bytes before peer policy
    /// succeeds. The request deadline includes transport setup and handshake.
    /// Run expensive providers on the owner's worker, outside camera capture.
    pub fn with_transport(
        listener: TcpListener,
        mut buffer: B,
        options: Options,
        transport: F,
    ) -> io::Result<Self> {
        if buffer.as_ref().is_empty()
            || buffer.as_ref().len() != buffer.as_mut().len()
            || options.request_timeout_ms == 0
            || options.send_deadline_ms == 0
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid streaming capacity or deadline",
            ));
        }
        listener.set_nonblocking(true)?;
        let mut header = Vec::new();
        header
            .try_reserve_exact(crate::HEADER_CAPACITY)
            .map_err(|_| io::Error::other("metadata storage unavailable"))?;
        header.resize(crate::HEADER_CAPACITY, 0);
        Ok(Self {
            listener,
            transport,
            buffer: Some(buffer),
            header: Some(header.into_boxed_slice()),
            client: None,
            options,
            stats: ServerStats::default(),
            last_now_ms: None,
            request_bytes: [0; REQUEST_CAPACITY],
        })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }
    pub fn stats(&self) -> ServerStats {
        self.stats
    }

    /// Release an active viewer, for example when the owner observes link loss.
    /// The listener and the same frame buffer remain available for reconnect.
    pub fn disconnect(&mut self) {
        if let Some(client) = self.client.take() {
            let _ = self.close(client, false);
        }
    }

    /// `now_ms` must not go backwards. A clock violation closes the client and
    /// increments errors; polling can resume once the previous time is reached.
    /// Sequences must increase; each new viewer starts with sequence zero as its
    /// lower bound. Future captures and captures older than the age cap are skipped.
    pub fn poll(&mut self, now_ms: u64, latest: Option<Frame<'_>>) -> io::Result<Progress> {
        self.poll_with_metadata(now_ms, latest, None)
    }
    pub fn poll_with_metadata(
        &mut self,
        now_ms: u64,
        latest: Option<Frame<'_>>,
        metadata: Option<Metadata<'_>>,
    ) -> io::Result<Progress> {
        let mut progress = Progress::default();
        if self.last_now_ms.is_some_and(|last| now_ms < last) {
            if let Some(client) = self.client.take() {
                let _ = self.close(client, false);
            }
            self.stats.errors = self.stats.errors.saturating_add(1);
            self.stats.last_failure = Some(Failure::Clock);
            return Ok(progress);
        }
        self.last_now_ms = Some(now_ms);
        match self.listener.accept() {
            Ok((socket, _)) if self.client.is_some() => {
                drop(socket);
                self.stats.rejected_busy = self.stats.rejected_busy.saturating_add(1);
            }
            Ok((socket, _)) => {
                self.stats.accepted = self.stats.accepted.saturating_add(1);
                let configured = socket
                    .set_nonblocking(true)
                    .and_then(|()| socket.set_nodelay(true))
                    .and_then(|()| (self.transport)(socket));
                match configured {
                    Err(error) => {
                        self.stats.errors = self.stats.errors.saturating_add(1);
                        self.stats.last_failure = Some(Failure::Configure(error.kind()));
                        self.stats.disconnected = self.stats.disconnected.saturating_add(1);
                    }
                    Ok(socket) => {
                        self.client = Some(Client::Request(Request {
                            socket,
                            length: 0,
                            deadline: now_ms.saturating_add(self.options.request_timeout_ms),
                        }));
                    }
                }
            }
            Err(error) if retryable(&error) => {}
            Err(error) => return Err(error),
        }
        if let Some(client) = self.client.take() {
            self.client = self.step(client, now_ms, latest, metadata, &mut progress);
        }
        progress.active = self.client.is_some();
        Ok(progress)
    }

    fn step(
        &mut self,
        client: Client<B, S>,
        now: u64,
        latest: Option<Frame<'_>>,
        metadata: Option<Metadata<'_>>,
        progress: &mut Progress,
    ) -> Option<Client<B, S>> {
        match client {
            Client::Request(mut request) => {
                if now >= request.deadline {
                    return self.fail(Client::Request(request), Failure::RequestDeadline);
                }
                match request
                    .socket
                    .read(&mut self.request_bytes[request.length..])
                {
                    Ok(0) => return self.close(Client::Request(request), false),
                    Ok(count) => request.length += count,
                    Err(error) if retryable(&error) => return Some(Client::Request(request)),
                    Err(error) => {
                        return self.fail(Client::Request(request), Failure::Read(error.kind()))
                    }
                }
                let kind = if let Some(end) = self.request_bytes[..request.length]
                    .windows(4)
                    .position(|bytes| bytes == b"\r\n\r\n")
                {
                    match request_path(&self.request_bytes[..end]) {
                        Some(path @ ("/stream" | "/stream.mjpg" | "/stream?metadata=1")) => {
                            let buffer = self
                                .buffer
                                .take()
                                .expect("idle server owns its frame buffer");
                            return Some(Client::Streaming(Streaming {
                                session: MjpegSession::with_header(
                                    request.socket,
                                    buffer,
                                    self.header.take().expect("idle server owns header storage"),
                                    now,
                                    self.options.send_deadline_ms,
                                ),
                                last_offered: 0,
                                read_closed: false,
                                last_write_ms: now,
                                ready: false,
                                metadata_enabled: path == "/stream?metadata=1",
                                metadata_revision: 0,
                            }));
                        }
                        Some("/clock") => ReplyKind::Clock,
                        Some("/") => ReplyKind::Viewer,
                        Some(_) => ReplyKind::Error(b"404 Not Found"),
                        None => ReplyKind::Error(b"400 Bad Request"),
                    }
                } else if request.length == REQUEST_CAPACITY {
                    ReplyKind::Error(b"431 Request Header Fields Too Large")
                } else {
                    return Some(Client::Request(request));
                };
                if matches!(kind, ReplyKind::Error(_)) {
                    self.stats.errors = self.stats.errors.saturating_add(1);
                    self.stats.last_failure = Some(Failure::InvalidRequest);
                }
                Some(Client::Reply(Reply::new(
                    request.socket,
                    kind,
                    now,
                    self.options.send_deadline_ms,
                )))
            }
            Client::Reply(mut reply) => {
                if now >= reply.deadline {
                    return self.fail(Client::Reply(reply), Failure::ResponseDeadline);
                }
                match reply
                    .socket
                    .write(&reply.bytes[reply.position..reply.length])
                {
                    Ok(0) => {
                        return self.fail(
                            Client::Reply(reply),
                            Failure::Write(io::ErrorKind::WriteZero),
                        )
                    }
                    Ok(count) => {
                        reply.position += count;
                        progress.bytes_written += count;
                    }
                    Err(error) if retryable(&error) => return Some(Client::Reply(reply)),
                    Err(error) => {
                        return self.fail(Client::Reply(reply), Failure::Write(error.kind()))
                    }
                }
                if reply.position == reply.length {
                    self.close(Client::Reply(reply), false)
                } else {
                    Some(Client::Reply(reply))
                }
            }
            Client::Streaming(mut stream) => {
                if self
                    .options
                    .idle_timeout_ms
                    .is_some_and(|idle| now.saturating_sub(stream.last_write_ms) >= idle)
                {
                    return self.fail(Client::Streaming(stream), Failure::IdleDeadline);
                }
                if !stream.read_closed {
                    match stream.session.writer_mut().read(&mut [0; 1]) {
                        // A TCP FIN closes only the request direction. The
                        // viewer may still read; writes/deadlines detect loss
                        // of the other direction without another descriptor.
                        Ok(0) => stream.read_closed = true,
                        Ok(_) => {
                            return self.fail(Client::Streaming(stream), Failure::UnexpectedInput)
                        }
                        Err(error) if retryable(&error) => {}
                        Err(error) => {
                            return self
                                .fail(Client::Streaming(stream), Failure::Read(error.kind()))
                        }
                    }
                }
                if stream.ready {
                    if let Some(frame) = latest.filter(|frame| {
                        frame.sequence > stream.last_offered
                            && now
                                .checked_sub(frame.capture.timestamp_ms)
                                .is_some_and(|age| {
                                    self.options
                                        .max_frame_age_ms
                                        .is_none_or(|limit| age <= limit)
                                })
                    }) {
                        let metadata = metadata.filter(|m| {
                            stream.metadata_enabled && m.revision > stream.metadata_revision
                        });
                        match stream.session.offer_with_metadata(frame, now, metadata) {
                            OfferResult::Accepted => {
                                stream.last_offered = frame.sequence;
                                progress.frame_accepted = true;
                                if let Some(m) = metadata {
                                    stream.metadata_revision = m.revision;
                                }
                            }
                            OfferResult::Busy => {}
                            _ => {
                                return self.fail(Client::Streaming(stream), Failure::InvalidFrame)
                            }
                        }
                    }
                }
                let before = stream.session.stats();
                let result = stream.session.poll(now, WRITE_BUDGET);
                let after = stream.session.stats();
                let completed = after.completed.saturating_sub(before.completed);
                self.stats.frames_completed = self.stats.frames_completed.saturating_add(completed);
                progress.frames_completed += completed;
                let bytes = after.bytes_written.saturating_sub(before.bytes_written) as usize;
                progress.bytes_written += bytes;
                if bytes != 0 {
                    stream.last_write_ms = now;
                }
                match result {
                    Ok(result) => stream.ready = result.ready,
                    Err(error) => {
                        return self.fail(Client::Streaming(stream), Failure::Stream(error))
                    }
                }
                Some(Client::Streaming(stream))
            }
        }
    }

    fn fail(&mut self, client: Client<B, S>, reason: Failure) -> Option<Client<B, S>> {
        self.stats.last_failure = Some(reason);
        self.close(client, true)
    }

    fn close(&mut self, client: Client<B, S>, failed: bool) -> Option<Client<B, S>> {
        if let Client::Streaming(stream) = client {
            let (socket, buffer, header, _) = stream.session.into_parts_with_header();
            drop(socket);
            self.buffer = Some(buffer);
            self.header = Some(header);
        }
        self.stats.disconnected = self.stats.disconnected.saturating_add(1);
        self.stats.errors = self.stats.errors.saturating_add(u64::from(failed));
        None
    }
}

impl<S> Reply<S> {
    fn new(socket: S, kind: ReplyKind, now: u64, timeout: u64) -> Self {
        let mut reply = Self {
            socket,
            bytes: [0; 256],
            length: 0,
            position: 0,
            deadline: now.saturating_add(timeout),
        };
        let output = &mut reply.bytes;
        let cursor = &mut reply.length;
        append(output, cursor, b"HTTP/1.1 ");
        append(
            output,
            cursor,
            match kind {
                ReplyKind::Error(status) => status,
                _ => b"200 OK",
            },
        );
        append(output, cursor, b"\r\nConnection: close\r\nContent-Length: ");
        append_number(
            output,
            cursor,
            if matches!(kind, ReplyKind::Viewer) {
                VIEWER.len() as u64
            } else {
                0
            },
        );
        if matches!(kind, ReplyKind::Clock) {
            append(output, cursor, b"\r\nX-Clock-Ms: ");
            append_number(output, cursor, now);
        } else if matches!(kind, ReplyKind::Viewer) {
            append(
                output,
                cursor,
                b"\r\nContent-Type: text/html; charset=utf-8",
            );
        }
        append(output, cursor, b"\r\n\r\n");
        if matches!(kind, ReplyKind::Viewer) {
            append(output, cursor, VIEWER);
        }
        reply
    }
}

fn retryable(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}

fn request_path(bytes: &[u8]) -> Option<&str> {
    let text = std::str::from_utf8(bytes).ok()?;
    if text
        .bytes()
        .any(|byte| byte.is_ascii_control() && !matches!(byte, b'\r' | b'\n' | b'\t'))
    {
        return None;
    }
    let mut lines = text.split("\r\n");
    let mut words = lines.next()?.split(' ');
    if words.next()? != "GET" {
        return None;
    }
    let path = words.next()?;
    if !path.starts_with('/')
        || path.bytes().any(|byte| byte <= b' ' || byte >= 127)
        || !matches!(words.next()?, "HTTP/1.0" | "HTTP/1.1")
        || words.next().is_some()
    {
        return None;
    }
    for line in lines {
        let (name, value) = line.split_once(':')?;
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
            || value.bytes().any(|byte| byte == b'\r' || byte == b'\n')
            || name.eq_ignore_ascii_case("transfer-encoding")
            || (name.eq_ignore_ascii_case("content-length") && value.trim() != "0")
        {
            return None;
        }
    }
    Some(path)
}
